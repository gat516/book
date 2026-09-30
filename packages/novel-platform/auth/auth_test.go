package auth

import (
	"context"
	"fmt"
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

// Exercise the real transaction locks under book_auth, using only the disposable
// PLATFORM_TEST_DATABASE_URL supplied by tests/hosted/run.sh.
func TestPublicSignupCapacityAndConcurrentLogins(t *testing.T) {
	dsn := os.Getenv("PLATFORM_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("PLATFORM_TEST_DATABASE_URL not set")
	}
	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.MaxConns = 12
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
		_, err := c.Exec(ctx, "SET ROLE book_auth")
		return err
	}
	db, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	prefix := "signup-test-" + Token()
	defer db.Exec(ctx, "DELETE FROM account WHERE left(google_subject,length($1))=$1", prefix)
	login := func(subject string, verified bool, invite string) *httptest.ResponseRecorder {
		s := &Server{DB: db, Origin: "https://books.example.test", Provider: fakeGoogle{Identity{subject, subject + "@example.test", verified}}}
		state := Token()
		var invitation any
		if invite != "" {
			invitation = Hash(invite)
		}
		_, err := db.Exec(ctx, "INSERT INTO account_oauth_state(state_hash,nonce,verifier,invitation_hash,expires_at) VALUES($1,$2,$3,$4,now()+interval '1 minute')", Hash(state), Token(), Token(), invitation)
		out := httptest.NewRecorder()
		if err != nil {
			t.Error(err)
			out.Code = 500
			return out
		}
		r := httptest.NewRequest("GET", "/auth/callback?code=test&state="+state, nil)
		r.AddCookie(&http.Cookie{Name: stateCookie, Value: state})
		s.callback(out, r)
		return out
	}
	if out := login(prefix+"-unverified", false, ""); out.Code != 403 {
		t.Fatal("unverified identity registered")
	}
	if out := login(prefix+"-invalid-invite", true, "invalid"); out.Code != 403 {
		t.Fatal("invalid invitation accepted")
	}
	// Two first logins for one subject must both succeed with one private account.
	results := make(chan *httptest.ResponseRecorder, 8)
	for i := 0; i < 8; i++ {
		go func() { results <- login(prefix+"-same", true, "") }()
	}
	for i := 0; i < 8; i++ {
		if out := <-results; out.Code != 303 {
			t.Fatalf("concurrent first login: %d %s", out.Code, out.Body.String())
		}
	}
	var count int
	if err := db.QueryRow(ctx, "SELECT count(*) FROM account WHERE google_subject=$1", prefix+"-same").Scan(&count); err != nil || count != 1 {
		t.Fatalf("expected one account: count=%d error=%v", count, err)
	}
	var id string
	if err := db.QueryRow(ctx, "SELECT id::text FROM account WHERE google_subject=$1", prefix+"-same").Scan(&id); err != nil || id == tenant.LegacyAccount {
		t.Fatal("public signup did not create a private owner")
	}
	if err := db.QueryRow(ctx, "SELECT count(*) FROM account WHERE google_subject IS NOT NULL").Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count >= signupLimit {
		t.Fatal("disposable test database is already full")
	}
	_, err = db.Exec(ctx, "INSERT INTO account(email,google_subject) SELECT $1 || '-filler-' || n || '@example.test', $1 || '-filler-' || n FROM generate_series(1,$2::int) AS n", prefix, signupLimit-1-count)
	if err != nil {
		t.Fatal(err)
	}
	// Different Google subjects racing for the last seat may register exactly one.
	for i := 0; i < 8; i++ {
		subject := fmt.Sprintf("%s-last-%d", prefix, i)
		go func() { results <- login(subject, true, "") }()
	}
	admitted, rejected := 0, 0
	for i := 0; i < 8; i++ {
		out := <-results
		switch out.Code {
		case 303:
			admitted++
		case 403:
			if !strings.Contains(out.Body.String(), "signups are full") {
				t.Errorf("unexpected rejection: %s", out.Body.String())
			}
			rejected++
		default:
			t.Errorf("unexpected signup response: %d %s", out.Code, out.Body.String())
		}
	}
	if admitted != 1 || rejected != 7 {
		t.Fatalf("last seat: admitted=%d rejected=%d", admitted, rejected)
	}
	if err := db.QueryRow(ctx, "SELECT count(*) FROM account WHERE google_subject IS NOT NULL").Scan(&count); err != nil || count != signupLimit {
		t.Fatalf("capacity: count=%d error=%v", count, err)
	}
	if out := login(prefix+"-same", true, ""); out.Code != 303 {
		t.Fatalf("existing user blocked at capacity: %s", out.Body.String())
	}
	invite := Token()
	_, err = db.Exec(ctx, "INSERT INTO account_invitation(token_hash,email,expires_at) VALUES($1,$2,now()+interval '1 hour')", Hash(invite), prefix+"-invited@example.test")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Exec(ctx, "DELETE FROM account_invitation WHERE token_hash=$1", Hash(invite))
	if out := login(prefix+"-invited", true, invite); out.Code != 403 {
		t.Fatal("invitation bypassed capacity")
	}
	var consumed bool
	if err := db.QueryRow(ctx, "SELECT consumed_at IS NOT NULL FROM account_invitation WHERE token_hash=$1", Hash(invite)).Scan(&consumed); err != nil || consumed {
		t.Fatal("rejected signup consumed its invitation")
	}
}
