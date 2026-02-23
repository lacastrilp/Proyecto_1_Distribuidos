package gateway

import (
	"sync"

	"github.com/google/uuid"
)

type Client struct {
	ID   string
	Hub  *Hub
	Send chan []byte
}

type Hub struct {
	Rooms      map[string]map[*Client]bool // roomID -> clients
	Register   chan *Client
	Unregister chan *Client
	Broadcast  chan BroadcastMessage
	mu         sync.RWMutex
}

type BroadcastMessage struct {
	RoomID  string
	Message []byte
}

func NewHub() *Hub {
	return &Hub{
		Rooms:      make(map[string]map[*Client]bool),
		Register:   make(chan *Client),
		Unregister: make(chan *Client),
		Broadcast:  make(chan BroadcastMessage),
	}
}

func (h *Hub) Run() {
	for {
		select {
		case client := <-h.Register:
			h.mu.Lock()
			if _, ok := h.Rooms[client.ID]; !ok { // por simplicidad usamos userId como room temporal
				h.Rooms[client.ID] = make(map[*Client]bool)
			}
			h.Rooms[client.ID][client] = true
			h.mu.Unlock()

		case client := <-h.Unregister:
			h.mu.Lock()
			if clients, ok := h.Rooms[client.ID]; ok {
				delete(clients, client)
				close(client.Send)
				if len(clients) == 0 {
					delete(h.Rooms, client.ID)
				}
			}
			h.mu.Unlock()

		case msg := <-h.Broadcast:
			h.mu.RLock()
			if clients, ok := h.Rooms[msg.RoomID]; ok {
				for client := range clients {
					select {
					case client.Send <- msg.Message:
					default:
						close(client.Send)
						delete(clients, client)
					}
				}
			}
			h.mu.RUnlock()
		}
	}
}
