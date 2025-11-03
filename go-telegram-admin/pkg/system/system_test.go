package system

import (
	"os/exec"
	"testing"
)

// This is a placeholder for a more comprehensive test suite.
// Testing system-level functions often requires mocking or a controlled environment.
// For the purpose of this project, we will rely on manual testing of the CLI.

func TestAddUser(t *testing.T) {
	// This test is a placeholder and won't run without a proper mock.
	// In a real-world scenario, you would use a library to mock exec.Command.
	t.Skip("skipping test that requires system modification")

	err := AddUser("testuser", "password")
	if err != nil {
		t.Fatalf("failed to add user: %v", err)
	}

	// Verify that the user was created
	_, err = exec.LookPath("testuser")
	if err != nil {
		t.Errorf("user 'testuser' not found after AddUser")
	}

	err = DeleteUser("testuser")
	if err != nil {
		t.Fatalf("failed to delete user: %v", err)
	}
}
