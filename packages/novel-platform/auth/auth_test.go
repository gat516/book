package auth

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"novel-engine/platform/tenant"
)

type fakeGoogle struct{ identity Identity }

func (f fakeGoogle) LoginURL(state, nonce, verifier string) string {
	return "https://accounts.example.test/login?state=" + state
}
func (f fakeGoogle) Exchange(context.Context, string, string, string) (Identity, error) {
	return f.identity, nil
}
func TestMutationRequiresOriginAndCSRF(t *testing.T) {
	s := &Server{Origin: "https://books.example.test"}
	session := Session{CSRF: Token()}
	for _, v := range []struct {
		origin, csrf string
		want         bool
	}{{s.Origin, session.CSRF, true}, {"https://evil.test", session.CSRF, false}, {s.Origin, "", false}, {"", session.CSRF, false}} {
		r := httptest.NewRequest("POST", "/novels", nil)
		r.Header.Set("Origin", v.origin)
		r.Header.Set("X-CSRF-Token", v.csrf)
		if s.validMutation(r, session) != v.want {
			t.Fatal("mutation guard failed")
		}
	}
}
func TestInvitedLoginAndSessionRevocation(t *testing.T) {
	dsn := os.Getenv("PLATFORM_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("PLATFORM_TEST_DATABASE_URL not set")
	}
	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error { _, err := c.Exec(ctx, "SET ROLE book_auth"); return err }
	db, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	email := Token() + "@example.test"
	subject := Token()
	invite := Token()
	_, err = db.Exec(ctx, "INSERT INTO account_invitation(token_hash,email,expires_at) VALUES($1,$2,now()+interval '1 hour')", Hash(invite), email)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Exec(ctx, "DELETE FROM account_invitation WHERE token_hash=$1", Hash(invite))
	defer db.Exec(ctx, "DELETE FROM account WHERE google_subject=$1", subject)
	s := &Server{DB: db, Origin: "https://books.example.test", Provider: fakeGoogle{Identity{subject, email, true}}}
	called := false
	handler := s.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		if tenant.Account(r.Context()) == "" {
			t.Fatal("missing authenticated identity")
		}
		if r.Header.Get(tenant.Header) != "" || r.Header.Get("X-Reader-ID") != "" {
			t.Fatal("forged identity survived")
		}
		w.WriteHeader(204)
	}))
	login := httptest.NewRecorder()
	handler.ServeHTTP(login, httptest.NewRequest("GET", "/auth/login?invite="+invite, nil))
	redirect, _ := url.Parse(login.Header().Get("Location"))
	state := redirect.Query().Get("state")
	if state == "" {
		t.Fatalf("no login redirect: %s", login.Body.String())
	}
	callback := httptest.NewRequest("GET", "/auth/callback?code=test&state="+state, nil)
	callback.AddCookie(login.Result().Cookies()[0])
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, callback)
	if out.Code != 303 {
		t.Fatalf("login failed: %s", out.Body.String())
	}
	var sessionCookie *http.Cookie
	for _, c := range out.Result().Cookies() {
		if c.Name == cookieName {
			sessionCookie = c
		}
	}
	if sessionCookie == nil || !sessionCookie.Secure || !sessionCookie.HttpOnly {
		t.Fatal("session cookie unsafe")
	}
	sessionReq := httptest.NewRequest("GET", "/auth/session", nil)
	sessionReq.AddCookie(sessionCookie)
	session, err := s.session(sessionReq)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/novels", strings.NewReader("{}"))
	req.AddCookie(sessionCookie)
	req.Header.Set("Origin", s.Origin)
	req.Header.Set("X-CSRF-Token", session.CSRF)
	req.Header.Set(tenant.Header, tenant.LegacyAccount)
	req.Header.Set("X-Reader-ID", "forged")
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, req)
	if !called || result.Code != 204 {
		t.Fatalf("authenticated request failed: %d", result.Code)
	}
	replay := httptest.NewRecorder()
	handler.ServeHTTP(replay, callback)
	if replay.Code != 403 {
		t.Fatal("OAuth state replay accepted")
	}
	req.URL.Path = "/auth/revoke-sessions"
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, req)
	if result.Code != 200 {
		t.Fatal("revocation failed")
	}
	if _, err = s.session(sessionReq); err == nil {
		t.Fatal("revoked session still valid")
	}
}
