package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"k8s.io/apimachinery/pkg/util/yaml"
)

type Result struct {
	SchemaPass bool     `json:"schema_pass"`
	CELPass    bool     `json:"cel_pass"`
	Errors     []string `json:"errors"`
	PolicyName string   `json:"policy_name"`
	PolicyKind string   `json:"policy_kind"`
	APIVersion string   `json:"api_version"`
}

func main() {
	policyPath := flag.String("policy", "", "Path to the policy YAML file to validate")
	flag.Parse()

	if *policyPath == "" {
		fmt.Fprintln(os.Stderr, "usage: validate-policy --policy <path>")
		os.Exit(2)
	}

	data, err := os.ReadFile(*policyPath)
	if err != nil {
		emitError(fmt.Sprintf("failed to read file: %v", err))
		os.Exit(2)
	}

	// Reject multi-document YAML
	if bytes.Count(data, []byte("\n---")) > 0 {
		// Check if it's actually multi-doc (not just a --- at the start)
		docs := bytes.Split(data, []byte("\n---"))
		nonEmpty := 0
		for _, d := range docs {
			if len(bytes.TrimSpace(d)) > 0 {
				nonEmpty++
			}
		}
		if nonEmpty > 1 {
			emitError("multi-document YAML is not supported; provide a single policy")
			os.Exit(2)
		}
	}

	// Extract basic metadata before full validation
	name, kind, apiVersion := extractMetadata(data)

	result := Result{
		PolicyName: name,
		PolicyKind: kind,
		APIVersion: apiVersion,
	}

	schemaPass, celPass, errs := validatePolicy(data)
	result.SchemaPass = schemaPass
	result.CELPass = celPass
	result.Errors = errs
	if result.Errors == nil {
		result.Errors = []string{}
	}

	out, _ := json.MarshalIndent(result, "", "  ")
	fmt.Println(string(out))

	if !schemaPass || !celPass {
		os.Exit(1)
	}
	os.Exit(0)
}

func extractMetadata(data []byte) (name, kind, apiVersion string) {
	var meta struct {
		APIVersion string `json:"apiVersion" yaml:"apiVersion"`
		Kind       string `json:"kind" yaml:"kind"`
		Metadata   struct {
			Name string `json:"name" yaml:"name"`
		} `json:"metadata" yaml:"metadata"`
	}
	reader := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096)
	if err := reader.Decode(&meta); err == nil {
		name = meta.Metadata.Name
		kind = meta.Kind
		apiVersion = meta.APIVersion
	}
	return
}

func emitError(msg string) {
	result := Result{
		Errors: []string{msg},
	}
	out, _ := json.MarshalIndent(result, "", "  ")
	fmt.Println(string(out))
}
