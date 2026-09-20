package tenant

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInternalRequiresTokenAndAccount(t *testing.T) {
	t.Setenv("BOOK_MODE", "hosted")
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if Account(r.Context()) != LegacyAccount {
			t.Fatal("lost verified actor")
		}
		w.WriteHeader(204)
	})
	for _, tc := range []struct {
		token, id string
		status    int
	}{{"", "", 401}, {"Bearer secret", "", 401}, {"Bearer secret", "garbage", 401}, {"Bearer secret", LegacyAccount, 204}} {
		r := httptest.NewRequest("POST", "/novels", nil)
		r.Header.Set("Authorization", tc.token)
		r.Header.Set(Header, tc.id)
		w := httptest.NewRecorder()
		Internal("secret", next).ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("got %d want %d", w.Code, tc.status)
		}
	}
}
func TestHostedScopeFailsClosed(t *testing.T) {
	t.Setenv("BOOK_MODE", "hosted")
	if Scope(context.Background()) != "" {
		t.Fatal("implicit account")
	}
	if Scope(WithAccount(context.Background(), LegacyAccount)) != LegacyAccount {
		t.Fatal("missing scope")
	}
}
