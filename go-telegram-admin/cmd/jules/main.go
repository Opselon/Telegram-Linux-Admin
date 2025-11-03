package main

import (
	"fmt"
	"os"

	"github.com/Opselon/go-telegram-admin/pkg/config"
	"github.com/Opselon/go-telegram-admin/pkg/telegram"
	"github.com/spf13/cobra"
)

var (
	cfgFile string
)

var rootCmd = &cobra.Command{
	Use:   "jules",
	Short: "A Telegram bot for Linux server administration.",
}

var runCmd = &cobra.Command{
	Use:   "run",
	Short: "Runs the Telegram bot.",
	Run: func(cmd *cobra.Command, args []string) {
		cfg, err := config.LoadConfig(cfgFile)
		if err != nil {
			fmt.Printf("Error loading config: %v\n", err)
			os.Exit(1)
		}

		client := telegram.NewClient(cfg.TelegramBotToken)

		fmt.Println("Starting bot...")
		// Main loop to get updates from Telegram
		offset := 0
		for {
			updates, err := client.GetUpdates(offset, 60)
			if err != nil {
				fmt.Printf("Error getting updates: %v\n", err)
				continue
			}

			for _, update := range updates {
				offset = update.UpdateID + 1
				// Process the update
				fmt.Printf("Received message: %s\n", update.Message.Text)
				// Here you would add the logic to handle the commands
				client.SendMessage(fmt.Sprintf("%d", update.Message.Chat.ID), "I received your message!")
			}
		}
	},
}

func init() {
	cobra.OnInitialize(initConfig)
	rootCmd.AddCommand(runCmd)
	runCmd.PersistentFlags().StringVar(&cfgFile, "config", "/etc/go-telegram-admin.json", "config file")
}

func initConfig() {
	// Future-proofing for more complex configuration
}

func main() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
}
