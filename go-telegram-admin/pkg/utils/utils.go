package utils

// Contains checks if a string is present in a slice of strings.
func Contains(slice []string, str string) bool {
	for _, v := range slice {
		if v == str {
			return true
		}
	}
	return false
}
