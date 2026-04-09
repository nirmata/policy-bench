package main

import (
	"fmt"
	"strings"

	"github.com/go-logr/logr"
	policiesv1 "github.com/kyverno/api/api/policies.kyverno.io/v1"
	policiesv1alpha1 "github.com/kyverno/api/api/policies.kyverno.io/v1alpha1"
	policiesv1beta1 "github.com/kyverno/api/api/policies.kyverno.io/v1beta1"
	"github.com/kyverno/kyverno/ext/resource/convert"
	"github.com/kyverno/kyverno/ext/resource/loader"
	dpolcompiler "github.com/kyverno/kyverno/pkg/cel/policies/dpol/compiler"
	gpolcompiler "github.com/kyverno/kyverno/pkg/cel/policies/gpol/compiler"
	mpolcompiler "github.com/kyverno/kyverno/pkg/cel/policies/mpol/compiler"
	vpolcompiler "github.com/kyverno/kyverno/pkg/cel/policies/vpol/compiler"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/kubectl-validate/pkg/openapiclient"
)

var (
	vpolV1alpha1 = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("ValidatingPolicy")
	dpolV1alpha1 = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("DeletingPolicy")
	gpolV1alpha1 = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("GeneratingPolicy")
	mpolV1alpha1 = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("MutatingPolicy")
	ivpolV1alpha1  = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("ImageValidatingPolicy")
	ndpolV1alpha1  = schema.GroupVersion(policiesv1alpha1.GroupVersion).WithKind("NamespacedDeletingPolicy")

	vpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("ValidatingPolicy")
	dpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("DeletingPolicy")
	gpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("GeneratingPolicy")
	mpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("MutatingPolicy")
	ivpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("ImageValidatingPolicy")
	ndpolV1beta1  = schema.GroupVersion(policiesv1beta1.GroupVersion).WithKind("NamespacedDeletingPolicy")

	vpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("ValidatingPolicy")
	dpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("DeletingPolicy")
	gpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("GeneratingPolicy")
	mpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("MutatingPolicy")
	ivpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("ImageValidatingPolicy")
	ndpolV1  = schema.GroupVersion(policiesv1.GroupVersion).WithKind("NamespacedDeletingPolicy")

)

func validatePolicy(policyBytes []byte) (bool, bool, []string, string) {
	ctrl.SetLogger(logr.Discard())

	schemaFS, err := SchemaFiles()
	if err != nil {
		return false, false, []string{fmt.Sprintf("failed to load schemas: %v", err)}, "schema_load"
	}

	ldr, err := loader.New(
		openapiclient.NewComposite(
			openapiclient.NewLocalSchemaFiles(schemaFS),
		),
	)
	if err != nil {
		return false, false, []string{fmt.Sprintf("failed to create loader: %v", err)}, "loader_init"
	}

	gvk, object, err := ldr.Load(policyBytes)
	if err != nil {
		// Classify failure by inspecting error text from kubectl-validate/loader.
		// These substrings are not a stable API — update if upstream wording changes.
		stage := "schema_validation"
		errMsg := err.Error()
		if strings.Contains(errMsg, "failed to parse document") {
			if strings.Contains(errMsg, "failed to retrieve validator") {
				stage = "schema_lookup"
			} else {
				stage = "yaml_parse"
			}
		}
		return false, false, []string{fmt.Sprintf("schema validation failed: %v", err)}, stage
	}

	switch gvk {

	case vpolV1alpha1, vpolV1beta1, vpolV1:
		typed, err := convert.To[policiesv1beta1.ValidatingPolicy](object)
		if err != nil {
			return true, false, []string{fmt.Sprintf("type conversion failed: %v", err)}, "type_conversion"
		}
		if errs := checkVpolMessageExpressions(typed); len(errs) > 0 {
			return true, false, errs, "cel_compile"
		}
		compiler := vpolcompiler.NewCompiler()
		_, errorList := compiler.Compile(typed, nil)
		if errorList != nil {
			if err := errorList.ToAggregate(); err != nil {
				return true, false, []string{fmt.Sprintf("CEL compilation failed: %v", err)}, "cel_compile"
			}
		}
		return true, true, nil, "passed"

	case mpolV1alpha1, mpolV1beta1, mpolV1:
		typed, err := convert.To[policiesv1beta1.MutatingPolicy](object)
		if err != nil {
			return true, false, []string{fmt.Sprintf("type conversion failed: %v", err)}, "type_conversion"
		}
		compiler := mpolcompiler.NewCompiler()
		_, errorList := compiler.Compile(typed, nil)
		if errorList != nil {
			if err := errorList.ToAggregate(); err != nil {
				return true, false, []string{fmt.Sprintf("CEL compilation failed: %v", err)}, "cel_compile"
			}
		}
		return true, true, nil, "passed"

	case gpolV1alpha1, gpolV1beta1, gpolV1:
		typed, err := convert.To[policiesv1beta1.GeneratingPolicy](object)
		if err != nil {
			return true, false, []string{fmt.Sprintf("type conversion failed: %v", err)}, "type_conversion"
		}
		compiler := gpolcompiler.NewCompiler()
		_, errorList := compiler.Compile(typed, nil)
		if errorList != nil {
			if err := errorList.ToAggregate(); err != nil {
				return true, false, []string{fmt.Sprintf("CEL compilation failed: %v", err)}, "cel_compile"
			}
		}
		return true, true, nil, "passed"

	case dpolV1alpha1, dpolV1beta1, dpolV1, ndpolV1alpha1, ndpolV1beta1, ndpolV1:
		typed, err := convert.To[policiesv1beta1.DeletingPolicy](object)
		if err != nil {
			return true, false, []string{fmt.Sprintf("type conversion failed: %v", err)}, "type_conversion"
		}
		compiler := dpolcompiler.NewCompiler()
		_, errorList := compiler.Compile(typed, nil)
		if errorList != nil {
			if err := errorList.ToAggregate(); err != nil {
				return true, false, []string{fmt.Sprintf("CEL compilation failed: %v", err)}, "cel_compile"
			}
		}
		return true, true, nil, "passed"

	case ivpolV1alpha1, ivpolV1beta1, ivpolV1:
		// ivpol CEL compilation requires ImageContext + SecretLister fakes.
		// Schema passed via loader; CEL not tested.
		return true, false, []string{"ImageValidatingPolicy CEL compilation not yet supported"}, "cel_compile"
	}

	return false, false, []string{fmt.Sprintf("unknown or unsupported policy type: %s", gvk.String())}, "unknown_gvk"
}

func checkVpolMessageExpressions(typed *policiesv1beta1.ValidatingPolicy) []string {
	var errs []string
	for i, v := range typed.Spec.Validations {
		expr := v.MessageExpression
		if expr == "" {
			continue
		}
		if strings.Contains(expr, ".orValue({})") || strings.Contains(expr, ".orValue([])") {
			errs = append(errs, fmt.Sprintf("validations[%d]: messageExpression contains invalid string conversion of map/list", i))
		}
	}
	return errs
}
