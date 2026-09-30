package main

import "fmt"

var categories = []string{"books", "electronics", "home", "sports", "toys"}

type Product struct {
	ID         int      `json:"id"`
	Name       string   `json:"name"`
	Category   string   `json:"category"`
	PriceCents int      `json:"priceCents"`
	Stock      int      `json:"stock"`
	Tags       []string `json:"tags"`
}

func productFor(id int) Product {
	category := categories[(id-1)%len(categories)]
	secondTag := "standard"
	if id%3 == 0 {
		secondTag = "featured"
	}
	parityTag := "odd"
	if id%2 == 0 {
		parityTag = "even"
	}
	return Product{ID: id, Name: fmt.Sprintf("Product %05d", id), Category: category, PriceCents: 500 + (id*7919)%50000, Stock: (id * 37) % 201, Tags: []string{category, secondTag, parityTag}}
}
