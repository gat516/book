package main

import (
	"crypto/subtle"
	"net/http"
)

// requireInternalToken gates one route with a shared-secret bearer token. This is the
// only auth ingest-api has — scoped narrowly to "reader-api is the only allowed caller"
// for novel creation, not general auth (main.go's doc comment: no auth/gate otherwise).
// Mirrors askai's hmac.compare_digest check (services/askai/askai/app.py) translated to
// Go's constant-time comparison primitive.
func requireInternalToken(token string, next http.Handler) http.Handler {
	expected := "Bearer " + token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get("Authorization")
		if subtle.ConstantTimeCompare([]byte(got), []byte(expected)) != 1 {
			writeErr(w, http.StatusUnauthorized, "invalid internal authorization")
			return
		}
		next.ServeHTTP(w, r)
	})
}
