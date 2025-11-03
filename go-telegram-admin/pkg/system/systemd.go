package system

import (
	"fmt"
	"os"
	"os/exec"
)

const serviceFile = `[Unit]
Description=Go Telegram Admin
After=network.target

[Service]
Type=simple
ExecStart=%s
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
`

// InstallService installs the systemd service.
func InstallService(executablePath string) error {
	serviceContent := fmt.Sprintf(serviceFile, executablePath)
	servicePath := "/etc/systemd/system/go-telegram-admin.service"

	err := os.WriteFile(servicePath, []byte(serviceContent), 0644)
	if err != nil {
		return err
	}

	cmd := exec.Command("systemctl", "daemon-reload")
	err = cmd.Run()
	if err != nil {
		return err
	}

	cmd = exec.Command("systemctl", "enable", "go-telegram-admin.service")
	err = cmd.Run()
	if err != nil {
		return err
	}

	cmd = exec.Command("systemctl", "start", "go-telegram-admin.service")
	err = cmd.Run()
	if err != nil {
		return err
	}

	return nil
}

// RemoveService removes the systemd service.
func RemoveService() error {
	cmd := exec.Command("systemctl", "stop", "go-telegram-admin.service")
	cmd.Run() // Ignore error

	cmd = exec.Command("systemctl", "disable", "go-telegram-admin.service")
	cmd.Run() // Ignore error

	err := os.Remove("/etc/systemd/system/go-telegram-admin.service")
	if err != nil {
		return err
	}

	cmd = exec.Command("systemctl", "daemon-reload")
	err = cmd.Run()
	if err != nil {
		return err
	}

	return nil
}
