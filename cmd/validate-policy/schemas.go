package main

import (
	"embed"
	"io/fs"
)

//go:embed schemas/openapi/v3
var schemas embed.FS

func SchemaFiles() (fs.FS, error) {
	return fs.Sub(schemas, "schemas/openapi/v3")
}
