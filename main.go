package main

import (
	"log"
	"net/http"

	"github.com/luisalejandro/Proyecto_1_Distribuidos/internal/gateway"
)

func main() {
	hub := gateway.NewHub() // manager de rooms y conexiones

	http.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		gateway.ServeWs(hub, w, r)
	})

	log.Println("🚀 Chat Gateway corriendo en :8080")
	log.Println("Conéctate con: ws://localhost:8080/ws?userId=123&token=abc")
	if err := http.ListenAndServe(":8080", nil); err != nil {
		log.Fatal("ListenAndServe: ", err)
	}
}
