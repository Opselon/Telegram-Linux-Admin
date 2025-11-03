package commands

import (
	"strings"
	"testing"
)

func TestExecCommand(t *testing.T) {
	output, err := ExecCommand("echo", "hello", "world")
	if err != nil {
		t.Fatalf("failed to execute command: %v", err)
	}

	expected := "hello world"
	if !strings.Contains(output, expected) {
		t.Errorf("expected output to contain '%s', got '%s'", expected, output)
	}
}
