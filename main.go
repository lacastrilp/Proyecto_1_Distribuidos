package main

import (
	"log"
	"net/http"
	"Proyecto_1_Distribuidos/internal/gateway"
)

func main() {
	hub := gateway.NewHub()

	// Iniciamos el loop del Hub en background
	go hub.Run()

	http.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		gateway.ServeWs(hub, w, r)
	})

	log.Println("🚀 Chat Gateway v2.0 corriendo en http://localhost:8080")
	log.Println("Ejemplo: ws://localhost:8080/ws?userId=luis&groupId=grupo1&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...")
	if err := http.ListenAndServe(":8080", nil); err != nil {
		log.Fatal(err)
	}
}