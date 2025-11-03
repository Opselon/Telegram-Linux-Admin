package commands

import (
	"os/exec"
)

// ExecCommand executes a shell command and returns its output.
func ExecCommand(name string, arg ...string) (string, error) {
	cmd := exec.Command(name, arg...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return "", err
	}
	return string(output), nil
}
