package main

import "testing"

func TestValidKey(t *testing.T) {
	for _, key := range []string{"a", "A-9_", strings128()} {
		if !validKey(key) {
			t.Fatalf("expected %q to be valid", key)
		}
	}
	for _, key := range []string{"", "a/b", "a.b", "a b", strings129(), "é"} {
		if validKey(key) {
			t.Fatalf("expected %q to be invalid", key)
		}
	}
}

func strings128() string {
	result := make([]byte, 128)
	for index := range result {
		result[index] = 'k'
	}
	return string(result)
}

func strings129() string {
	return strings128() + "k"
}
