package gateway

import (
	"log"
	"net/http"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

func ServeWs(hub *Hub, w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Println(err)
		return
	}

	userID := r.URL.Query().Get("userId")
	if userID == "" {
		userID = uuid.NewString()
	}

	client := &Client{
		ID:   userID,
		Hub:  hub,
		Send: make(chan []byte, 256),
	}

	hub.Register <- client

	// Goroutine para enviar mensajes
	go func() {
		defer func() {
			hub.Unregister <- client
			conn.Close()
		}()
		for msg := range client.Send {
			conn.WriteMessage(websocket.TextMessage, msg)
		}
	}()

	// Goroutine para recibir mensajes
	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			break
		}
		// Aquí iría la validación JWT + producir a Kafka en producción
		hub.Broadcast <- BroadcastMessage{
			RoomID:  userID, // en producción sería group_id o channel_id
			Message: message,
		}
	}
}
