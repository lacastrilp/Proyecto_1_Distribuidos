package gateway

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"github.com/segmentio/kafka-go"
)

var (
	upgrader = websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true },
	}

	redisClient = redis.NewClient(&redis.Options{
		Addr: "localhost:6379",
	})

	kafkaWriter = &kafka.Writer{
		Addr:     kafka.TCP("localhost:9092"),
		Topic:    "chat-messages",
		Balancer: &kafka.LeastBytes{},
	}
)

func ServeWs(hub *Hub, w http.ResponseWriter, r *http.Request) {
	// === JWT simple ===
	tokenStr := r.URL.Query().Get("token")
	if tokenStr == "" {
		http.Error(w, "Token requerido", 401)
		return
	}
	token, err := jwt.Parse(tokenStr, func(t *jwt.Token) (interface{}, error) {
		return []byte("supersecret"), nil
	})
	if err != nil || !token.Valid {
		http.Error(w, "Token inválido", 401)
		return
	}

	userID := r.URL.Query().Get("userId")
	groupID := r.URL.Query().Get("groupId")
	if userID == "" || groupID == "" {
		userID = uuid.NewString()
		groupID = "default"
	}

	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Println(err)
		return
	}

	client := &Client{
		ID:      userID,
		GroupID: groupID,
		Hub:     hub,
		Send:    make(chan []byte, 256),
	}

	hub.Register <- client

	// Presencia en Redis (TTL 30s)
	ctx := context.Background()
	redisClient.Set(ctx, "user:"+userID+":online", "1", 30*time.Second)

	go func() {
		ticker := time.NewTicker(20 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			redisClient.Set(ctx, "user:"+userID+":online", "1", 30*time.Second)
		}
	}()

	// Goroutine para enviar mensajes al cliente
	go func() {
		defer func() {
			hub.Unregister <- client
			redisClient.Del(ctx, "user:"+userID+":online")
			conn.Close()
		}()
		for msg := range client.Send {
			conn.WriteMessage(websocket.TextMessage, msg)
		}
	}()

	// Recibir mensajes del cliente
	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			break
		}

		// Publicar a Kafka (distribuido)
		msg := map[string]interface{}{
			"messageId": uuid.NewString(),
			"groupId":   groupID,
			"userId":    userID,
			"content":   string(message),
			"timestamp": time.Now().UnixMilli(),
		}
		msgBytes, _ := json.Marshal(msg)

		err = kafkaWriter.WriteMessages(context.Background(), kafka.Message{
			Key:   []byte(groupID),
			Value: msgBytes,
		})
		if err != nil {
			log.Printf("Kafka error: %v", err)
		}

		// Broadcast local (para prueba con 1 solo gateway)
		hub.Broadcast <- BroadcastMessage{GroupID: groupID, Message: message}
	}
}