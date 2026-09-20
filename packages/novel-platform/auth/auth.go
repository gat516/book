// Package auth implements the invite-only account boundary (§15), independent of chapters.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/oauth2"
	"novel-engine/platform/tenant"
)

const cookieName = "__Host-book-session"
const stateCookie = "__Host-book-login"

type Config struct{ Origin, ClientID, ClientSecret string }
type Identity struct {
	Subject, Email string
	Verified       bool
}
type Provider interface {
	LoginURL(state, nonce, verifier string) string
	Exchange(context.Context, string, string, string) (Identity, error)
}
type googleProvider struct {
	oauth    oauth2.Config
	verifier *oidc.IDTokenVerifier
}

func (p *googleProvider) LoginURL(state, nonce, verifier string) string {
	return p.oauth.AuthCodeURL(state, oidc.Nonce(nonce), oauth2.S256ChallengeOption(verifier))
}
func (p *googleProvider) Exchange(ctx context.Context, code, verifier, nonce string) (Identity, error) {
	tok, err := p.oauth.Exchange(ctx, code, oauth2.VerifierOption(verifier))
	if err != nil {
		return Identity{}, errors.New("Google exchange failed")
	}
	raw, ok := tok.Extra("id_token").(string)
	if !ok {
		return Identity{}, errors.New("missing identity")
	}
	id, err := p.verifier.Verify(ctx, raw)
	if err != nil {
		return Identity{}, errors.New("invalid identity")
	}
	if subtle.ConstantTimeCompare([]byte(id.Nonce), []byte(nonce)) != 1 {
		return Identity{}, errors.New("invalid nonce")
	}
	var claims struct {
		Email    string `json:"email"`
		Verified bool   `json:"email_verified"`
	}
	if err = id.Claims(&claims); err != nil {
		return Identity{}, err
	}
	return Identity{id.Subject, strings.ToLower(strings.TrimSpace(claims.Email)), claims.Verified}, nil
}

type Server struct {
	DB       *pgxpool.Pool
	Provider Provider
	Origin   string
}
type Session struct {
	ID        string `json:"id"`
	Email     string `json:"email"`
	CSRF      string `json:"csrf_token"`
	TokenHash string `json:"-"`
}

func Token() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}
func Hash(s string) string { b := sha256.Sum256([]byte(s)); return hex.EncodeToString(b[:]) }

func New(ctx context.Context, dsn string, c Config) (*Server, error) {
	u, err := url.Parse(c.Origin)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.User != nil {
		return nil, errors.New("APP_ORIGIN must be an HTTPS origin")
	}
	if c.ClientID == "" || c.ClientSecret == "" {
		return nil, errors.New("Google client configuration is required")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 4
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, "SET ROLE book_auth")
		return err
	}
	db, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	discovery, err := oidc.NewProvider(ctx, "https://accounts.google.com")
	if err != nil {
		db.Close()
		return nil, errors.New("Google discovery unavailable")
	}
	return &Server{DB: db, Origin: c.Origin, Provider: &googleProvider{oauth: oauth2.Config{ClientID: c.ClientID, ClientSecret: c.ClientSecret, Endpoint: discovery.Endpoint(), RedirectURL: c.Origin + "/api/auth/callback", Scopes: []string{oidc.ScopeOpenID, "email"}}, verifier: discovery.Verifier(&oidc.Config{ClientID: c.ClientID})}}, nil
}
func NewFromEnv(ctx context.Context, dsn string) (*Server, error) {
	return New(ctx, dsn, Config{os.Getenv("APP_ORIGIN"), os.Getenv("GOOGLE_CLIENT_ID"), os.Getenv("GOOGLE_CLIENT_SECRET")})
}
func (s *Server) Close() { s.DB.Close() }
func write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func fail(w http.ResponseWriter, status int, message string) {
	write(w, status, map[string]string{"error": message})
}
func setCookie(w http.ResponseWriter, name, value string, age int) {
	http.SetCookie(w, &http.Cookie{Name: name, Value: value, Path: "/", MaxAge: age, Secure: true, HttpOnly: true, SameSite: http.SameSiteLaxMode})
}
func (s *Server) session(r *http.Request) (Session, error) {
	c, err := r.Cookie(cookieName)
	if err != nil || len(c.Value) > 100 {
		return Session{}, errors.New("no session")
	}
	var result Session
	result.TokenHash = Hash(c.Value)
	err = s.DB.QueryRow(r.Context(), `SELECT a.id::text,COALESCE(a.email,''),s.csrf_token FROM account_session s JOIN account a ON a.id=s.account_id WHERE s.token_hash=$1 AND s.expires_at>now() AND a.status='active'`, result.TokenHash).Scan(&result.ID, &result.Email, &result.CSRF)
	return result, err
}
func (s *Server) validMutation(r *http.Request, session Session) bool {
	return r.Header.Get("Origin") == s.Origin && session.CSRF != "" && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-CSRF-Token")), []byte(session.CSRF)) == 1
}
func (s *Server) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Never trust an actor supplied by a browser (§0.3).
		r.Header.Del(tenant.Header)
		r.Header.Del("X-Reader-ID")
		w.Header().Set("Cache-Control", "private, no-store")
		w.Header().Set("Vary", "Cookie")
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		if r.Method == "GET" && r.URL.Path == "/auth/login" {
			s.login(w, r)
			return
		}
		if r.Method == "GET" && r.URL.Path == "/auth/callback" {
			s.callback(w, r)
			return
		}
		session, err := s.session(r)
		if err != nil {
			fail(w, 401, "sign in required")
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" && !s.validMutation(r, session) {
			fail(w, 403, "invalid request origin or CSRF token")
			return
		}
		r = r.WithContext(tenant.WithAccount(r.Context(), session.ID))
		switch {
		case r.Method == "GET" && r.URL.Path == "/auth/session":
			write(w, 200, session)
			return
		case r.Method == "DELETE" && r.URL.Path == "/auth/account":
			tx, err := s.DB.Begin(r.Context())
			if err != nil {
				fail(w, 503, "deletion unavailable")
				return
			}
			defer tx.Rollback(r.Context())
			if _, err = tx.Exec(r.Context(), "SELECT request_account_deletion($1)", session.ID); err == nil {
				_, err = tx.Exec(r.Context(), "DELETE FROM account_session WHERE account_id=$1", session.ID)
			}
			if err != nil {
				fail(w, 503, "deletion unavailable")
				return
			}
			if err = tx.Commit(r.Context()); err != nil {
				fail(w, 503, "deletion unavailable")
				return
			}
			setCookie(w, cookieName, "", -1)
			write(w, 202, map[string]bool{"deletion_requested": true})
			return
		case r.Method == "POST" && r.URL.Path == "/auth/logout":
			_, err = s.DB.Exec(r.Context(), "DELETE FROM account_session WHERE token_hash=$1", session.TokenHash)
			if err != nil {
				fail(w, 503, "logout unavailable")
				return
			}
			setCookie(w, cookieName, "", -1)
			write(w, 200, map[string]bool{"logged_out": true})
			return
		case r.Method == "POST" && r.URL.Path == "/auth/revoke-sessions":
			_, err = s.DB.Exec(r.Context(), "DELETE FROM account_session WHERE account_id=$1", session.ID)
			if err != nil {
				fail(w, 503, "session revocation unavailable")
				return
			}
			setCookie(w, cookieName, "", -1)
			write(w, 200, map[string]bool{"logged_out": true})
			return
		}
		next.ServeHTTP(w, r)
	})
}
func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	state, nonce, verifier := Token(), Token(), oauth2.GenerateVerifier()
	invite := r.URL.Query().Get("invite")
	if len(invite) > 100 {
		fail(w, 400, "invalid invitation")
		return
	}
	var invitation any
	if invite != "" {
		invitation = Hash(invite)
	}
	// State is bound to this browser as well as stored server-side; expires after 10 minutes.
	_, err := s.DB.Exec(r.Context(), "INSERT INTO account_oauth_state(state_hash,nonce,verifier,invitation_hash,expires_at) VALUES($1,$2,$3,$4,now()+interval '10 minutes')", Hash(state), nonce, verifier, invitation)
	if err != nil {
		fail(w, 503, "sign in unavailable")
		return
	}
	setCookie(w, stateCookie, state, 600)
	http.Redirect(w, r, s.Provider.LoginURL(state, nonce, verifier), http.StatusFound)
}
func (s *Server) callback(w http.ResponseWriter, r *http.Request) {
	state := r.URL.Query().Get("state")
	cookie, err := r.Cookie(stateCookie)
	if err != nil || state == "" || len(state) > 100 || subtle.ConstantTimeCompare([]byte(state), []byte(cookie.Value)) != 1 {
		fail(w, 403, "invalid login state")
		return
	}
	setCookie(w, stateCookie, "", -1)
	var nonce, verifier string
	var invitation *string
	err = s.DB.QueryRow(r.Context(), "DELETE FROM account_oauth_state WHERE state_hash=$1 AND expires_at>now() RETURNING nonce,verifier,invitation_hash", Hash(state)).Scan(&nonce, &verifier, &invitation)
	if err != nil {
		fail(w, 403, "expired login state")
		return
	}
	identity, err := s.Provider.Exchange(r.Context(), r.URL.Query().Get("code"), verifier, nonce)
	identity.Email = strings.ToLower(strings.TrimSpace(identity.Email))
	if err != nil || !identity.Verified || identity.Subject == "" || identity.Email == "" {
		fail(w, 403, "Google identity could not be verified")
		return
	}
	tx, err := s.DB.Begin(r.Context())
	if err != nil {
		fail(w, 503, "sign in unavailable")
		return
	}
	defer tx.Rollback(context.Background())
	var id, status string
	err = tx.QueryRow(r.Context(), "SELECT id::text,status FROM account WHERE google_subject=$1 FOR UPDATE", identity.Subject).Scan(&id, &status)
	if errors.Is(err, pgx.ErrNoRows) {
		if invitation == nil {
			fail(w, 403, "an invitation is required")
			return
		}
		var target *string
		err = tx.QueryRow(r.Context(), `UPDATE account_invitation SET consumed_at=now() WHERE token_hash=$1 AND lower(email)=$2 AND expires_at>now() AND consumed_at IS NULL RETURNING account_id::text`, *invitation, identity.Email).Scan(&target)
		if err != nil {
			fail(w, 403, "invitation is invalid or expired")
			return
		}
		if target != nil {
			err = tx.QueryRow(r.Context(), "UPDATE account SET email=$2,google_subject=$3 WHERE id=$1 AND google_subject IS NULL AND status='active' RETURNING id::text", *target, identity.Email, identity.Subject).Scan(&id)
		} else {
			err = tx.QueryRow(r.Context(), "INSERT INTO account(email,google_subject) VALUES($1,$2) RETURNING id::text", identity.Email, identity.Subject).Scan(&id)
		}
		if err != nil {
			fail(w, 403, "account could not be created")
			return
		}
	} else if err != nil {
		fail(w, 503, "sign in unavailable")
		return
	} else if status != "active" {
		fail(w, 403, "account is unavailable")
		return
	}
	token, csrf := Token(), Token()
	_, err = tx.Exec(r.Context(), "INSERT INTO account_session(token_hash,account_id,csrf_token,expires_at) VALUES($1,$2,$3,now()+interval '30 days')", Hash(token), id, csrf)
	if err == nil {
		err = tx.Commit(r.Context())
	}
	if err != nil {
		fail(w, 503, "sign in unavailable")
		return
	}
	setCookie(w, cookieName, token, 30*24*3600)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// LocalMiddleware gives the existing local library an explicit principal; it is never
// selected by a browser flag and the hosted deployment refuses missing OAuth config.
func LocalMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/auth/session" {
			write(w, 200, map[string]any{"id": tenant.LegacyAccount, "email": "Local reader", "csrf_token": "", "local": true})
			return
		}
		next.ServeHTTP(w, r.WithContext(tenant.WithAccount(r.Context(), tenant.LegacyAccount)))
	})
}
func (s Session) String() string { return fmt.Sprintf("account %s", s.ID) }
