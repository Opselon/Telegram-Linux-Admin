package shell

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestShellSession(t *testing.T) {
	session, err := NewSession()
	if err != nil {
		t.Fatalf("failed to create new session: %v", err)
	}

	// Test 'pwd'
	output, err := session.Execute("pwd")
	if err != nil {
		t.Fatalf("failed to execute 'pwd': %v", err)
	}

	cwd, _ := os.Getwd()
	if !strings.Contains(output, cwd) {
		t.Errorf("expected output to contain '%s', got '%s'", cwd, output)
	}

	// Test 'cd'
	// Create a temporary directory to 'cd' into
	tempDir, err := os.MkdirTemp("", "shell-test")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	_, err = session.Execute("cd " + tempDir)
	if err != nil {
		t.Fatalf("failed to execute 'cd': %v", err)
	}

	// Check that the session's cwd has changed
	if session.cwd != tempDir {
		t.Errorf("expected cwd to be '%s', got '%s'", tempDir, session.cwd)
	}

	// Test 'cd ..'
	_, err = session.Execute("cd ..")
	if err != nil {
		t.Fatalf("failed to execute 'cd ..': %v", err)
	}

	// Check that the session's cwd is the parent of the temp dir
	expectedParent := filepath.Dir(tempDir)
	if session.cwd != expectedParent {
		t.Errorf("expected cwd to be '%s', got '%s'", expectedParent, session.cwd)
	}
}
