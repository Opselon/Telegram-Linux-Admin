package utils

import "testing"

func TestContains(t *testing.T) {
	slice := []string{"a", "b", "c"}
	if !Contains(slice, "a") {
		t.Error("expected to find 'a'")
	}
	if Contains(slice, "d") {
		t.Error("expected not to find 'd'")
	}
}
