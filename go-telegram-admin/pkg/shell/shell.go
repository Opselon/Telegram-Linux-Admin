package shell

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Session represents a persistent shell session.
type Session struct {
	cwd string
	env []string
}

// NewSession creates a new shell session.
func NewSession() (*Session, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return nil, err
	}
	return &Session{
		cwd: cwd,
		env: os.Environ(),
	}, nil
}

// Execute executes a command in the shell session.
func (s *Session) Execute(command string) (string, error) {
	parts := strings.Fields(command)
	if len(parts) == 0 {
		return "", nil
	}

	if parts[0] == "cd" {
		if len(parts) == 1 {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			s.cwd = home
			return "", nil
		}

		// Try to change directory
		newDir := parts[1]
		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(s.cwd, newDir)
		}

		// Check if the directory exists
		info, err := os.Stat(newDir)
		if err != nil {
			return "", err
		}
		if !info.IsDir() {
			return "", errors.New("not a directory")
		}

		s.cwd = newDir
		return "", nil
	}

	cmd := exec.Command(parts[0], parts[1:]...)
	cmd.Dir = s.cwd
	cmd.Env = s.env

	output, err := cmd.CombinedOutput()
	if err != nil {
		return string(output), err
	}

	return string(output), nil
}
