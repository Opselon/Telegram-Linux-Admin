package system

import "os/exec"

// UFWEnable enables the UFW firewall.
func UFWEnable() error {
	cmd := exec.Command("ufw", "--force", "enable")
	return cmd.Run()
}

// UFWDisable disables the UFW firewall.
func UFWDisable() error {
	cmd := exec.Command("ufw", "--force", "disable")
	return cmd.Run()
}

// UFWAllow allows a port through the firewall.
func UFWAllow(port string) error {
	cmd := exec.Command("ufw", "allow", port)
	return cmd.Run()
}

// UFWDeny denies a port through the firewall.
func UFWDeny(port string) error {
	cmd := exec.Command("ufw", "deny", port)
	return cmd.Run()
}

// UFWStatus gets the status of the UFW firewall.
func UFWStatus() (string, error) {
	cmd := exec.Command("ufw", "status", "verbose")
	output, err := cmd.CombinedOutput()
	if err != nil {
		return "", err
	}
	return string(output), nil
}
