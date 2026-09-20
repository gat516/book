// Package tenant carries server-verified ownership, independent of spoiler clearance (§0.3).
package tenant

import (
	"context"
	"crypto/subtle"
	"errors"
	"net/http"
	"os"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type key struct{}

const LegacyAccount = "00000000-0000-4000-8000-000000000001"
const Header = "X-Account-ID"

func Hosted() bool { return os.Getenv("BOOK_MODE") == "hosted" }
func WithAccount(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, key{}, id)
}
func Account(ctx context.Context) string { s, _ := ctx.Value(key{}).(string); return s }
func ValidID(id string) bool {
	if len(id) != 36 {
		return false
	}
	for i, c := range id {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if c != '-' {
				return false
			}
		} else if !strings.ContainsRune("0123456789abcdefABCDEF", c) {
			return false
		}
	}
	return true
}
func Scope(ctx context.Context) string {
	if id := Account(ctx); id != "" {
		return id
	}
	if !Hosted() {
		return LegacyAccount
	}
	return ""
}

// Pool hooks reset scope on EVERY checkout. No account context survives a pooled request.
// Begin/BeginTx inherit this scope; the chapter gate remains transaction-local.
func ConfigurePool(cfg *pgxpool.Config) {
	previous := cfg.BeforeAcquire
	cfg.BeforeAcquire = func(ctx context.Context, conn *pgx.Conn) bool {
		if previous != nil && !previous(ctx, conn) {
			return false
		}
		_, err := conn.Exec(ctx, "SELECT set_config('app.account_id',$1,false), set_config('app.novel_id','',false), set_config('app.current_chapter','',false)", Scope(ctx))
		return err == nil
	}
}
func CheckOwnership(ctx context.Context, db *pgxpool.Pool, novel string) error {
	var ok bool
	err := db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM novel WHERE id=$1 AND owner_id=$2)", novel, Account(ctx)).Scan(&ok)
	if err != nil {
		return err
	}
	if !ok {
		return errors.New("not found")
	}
	return nil
}
func Forward(ctx context.Context, r *http.Request, token string) {
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set(Header, Account(ctx))
}
func Internal(token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		expected := "Bearer " + token
		if token == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(expected)) != 1 {
			http.Error(w, "unauthorized", 401)
			return
		}
		id := r.Header.Get(Header)
		if !Hosted() && id == "" {
			id = LegacyAccount
		}
		if !ValidID(id) {
			http.Error(w, "unauthorized", 401)
			return
		}
		next.ServeHTTP(w, r.WithContext(WithAccount(r.Context(), id)))
	})
}
