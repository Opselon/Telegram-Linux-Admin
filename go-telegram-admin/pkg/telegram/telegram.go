package telegram

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
)

var (
	apiEndpoint = "https://api.telegram.org/bot%s/%s"
)

// Client is a Telegram Bot API client.
type Client struct {
	token      string
	httpClient *http.Client
}

// NewClient creates a new Telegram Bot API client.
func NewClient(token string) *Client {
	return &Client{
		token:      token,
		httpClient: &http.Client{},
	}
}

// SendMessage sends a message to a chat.
func (c *Client) SendMessage(chatID string, text string) error {
	url := fmt.Sprintf(apiEndpoint, c.token, "sendMessage")

	message := struct {
		ChatID string `json:"chat_id"`
		Text   string `json:"text"`
	}{
		ChatID: chatID,
		Text:   text,
	}

	body, err := json.Marshal(message)
	if err != nil {
		return err
	}

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	return nil
}

// Update represents an incoming update.
type Update struct {
	UpdateID int     `json:"update_id"`
	Message  Message `json:"message"`
}

// Message represents a Telegram message.
type Message struct {
	MessageID int    `json:"message_id"`
	Text      string `json:"text"`
	Chat      Chat   `json:"chat"`
}

// Chat represents a Telegram chat.
type Chat struct {
	ID int `json:"id"`
}


// GetUpdates gets updates from the Telegram Bot API.
func (c *Client) GetUpdates(offset int, timeout int) ([]Update, error) {
	url := fmt.Sprintf(apiEndpoint, c.token, "getUpdates")

	params := struct {
		Offset  int `json:"offset"`
		Timeout int `json:"timeout"`
	}{
		Offset:  offset,
		Timeout: timeout,
	}

	body, err := json.Marshal(params)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	var result struct {
		OK     bool     `json:"ok"`
		Result []Update `json:"result"`
	}

	decoder := json.NewDecoder(resp.Body)
	err = decoder.Decode(&result)
	if err != nil {
		return nil, err
	}

	if !result.OK {
		return nil, fmt.Errorf("telegram API error")
	}

	return result.Result, nil
}

// SendDocument sends a document to a chat.
func (c *Client) SendDocument(chatID string, filePath string, caption string) error {
	// Implementation for sending a document
	return nil
}
