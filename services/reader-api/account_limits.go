package main

import (
	"context"
	"github.com/redis/go-redis/v9"
	"net/http"
	"novel-engine/platform/auth"
	"novel-engine/platform/tenant"
	"strings"
	"time"
)

// §15: a short lease bounds one account's interactive model work across API replicas.
func accountLimits(client *redis.Client, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !tenant.Hosted() || r.Method != "POST" || !strings.HasSuffix(r.URL.Path, "/ask") {
			next.ServeHTTP(w, r)
			return
		}
		account := tenant.Account(r.Context())
		if account == "" {
			writeError(w, 401, "sign in required")
			return
		}
		token := auth.Token()
		prefix := "limits:ask:" + account
		accepted, err := client.Eval(r.Context(), `if redis.call('EXISTS',KEYS[1])==1 then return 0 end
if tonumber(redis.call('GET',KEYS[2]) or '0')>=10 then return 0 end
local n=redis.call('INCR',KEYS[2]);if n==1 then redis.call('EXPIRE',KEYS[2],60) end
redis.call('SET',KEYS[1],ARGV[1],'EX',600);return 1`, []string{prefix + ":active", prefix + ":minute"}, token).Int()
		if err != nil {
			writeError(w, 503, "request limits unavailable")
			return
		}
		if accepted != 1 {
			w.Header().Set("Retry-After", "60")
			writeError(w, 429, "one active question and ten questions per minute are allowed")
			return
		}
		defer func() {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			client.Eval(ctx, `if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0`, []string{prefix + ":active"}, token)
		}()
		ctx, cancel := context.WithTimeout(r.Context(), 9*time.Minute)
		defer cancel()
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}
