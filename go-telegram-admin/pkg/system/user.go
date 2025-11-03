package system

import (
	"os/exec"
	"strings"
)

// AddUser adds a new system user.
func AddUser(username string, password string) error {
	cmd := exec.Command("useradd", "-m", "-s", "/bin/bash", username)
	err := cmd.Run()
	if err != nil {
		return err
	}

	cmd = exec.Command("chpasswd")
	cmd.Stdin = strings.NewReader(username + ":" + password)
	err = cmd.Run()
	if err != nil {
		return err
	}

	return nil
}

// DeleteUser deletes a system user.
func DeleteUser(username string) error {
	cmd := exec.Command("userdel", "-r", username)
	err := cmd.Run()
	if err != nil {
		return err
	}
	return nil
}
