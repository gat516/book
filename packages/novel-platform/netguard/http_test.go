package netguard

import (
	"context"
	"net/http"
	"net/netip"
	"testing"
	"time"
)

func TestPublicDestinations(t *testing.T) {
	for _, ip := range []string{"127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fc00::1", "100.64.0.1", "192.0.2.1"} {
		if Public(netip.MustParseAddr(ip)) {
			t.Errorf("allowed %s", ip)
		}
	}
	if !Public(netip.MustParseAddr("8.8.8.8")) {
		t.Fatal("public blocked")
	}
}
func TestApprovedEndpoint(t *testing.T) {
	for _, u := range []string{"http://models.example", "https://models.example:123", "https://models.example@evil.example", "https://evil.example", "https://models.example/?key=x"} {
		if AllowedURL(u, "models.example") == nil {
			t.Errorf("allowed %s", u)
		}
	}
	if err := AllowedURL("https://models.example/v1", "models.example"); err != nil {
		t.Fatal(err)
	}
}
func TestDialRejectsPrivate(t *testing.T) {
	client := Client(time.Second)
	_, err := client.Transport.(*http.Transport).DialContext(context.Background(), "tcp", "127.0.0.1:1234")
	if err == nil || err.Error() != "non-public destination blocked" {
		t.Fatalf("%v", err)
	}
}
