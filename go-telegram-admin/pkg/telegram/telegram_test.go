package telegram

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSendMessage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/bottoken/sendMessage" {
			t.Errorf("expected path /bottoken/sendMessage, got %s", r.URL.Path)
		}

		var message struct {
			ChatID string `json:"chat_id"`
			Text   string `json:"text"`
		}

		decoder := json.NewDecoder(r.Body)
		err := decoder.Decode(&message)
		if err != nil {
			t.Fatalf("failed to decode request body: %v", err)
		}

		if message.ChatID != "12345" {
			t.Errorf("expected chat id 12345, got %s", message.ChatID)
		}

		if message.Text != "hello" {
			t.Errorf("expected text 'hello', got %s", message.Text)
		}

		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"ok":true,"result":{}}`))
	}))
	defer server.Close()

	client := &Client{
		token:      "token",
		httpClient: server.Client(),
	}

	apiEndpoint = server.URL + "/bot%s/%s"

	err := client.SendMessage("12345", "hello")
	if err != nil {
		t.Fatalf("failed to send message: %v", err)
	}
}
