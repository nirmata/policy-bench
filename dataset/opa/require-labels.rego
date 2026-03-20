package kubernetes.admission.require_labels

# Deny resources missing required labels.
# Every namespace must have an "owner" and "environment" label.

import rego.v1

required_labels := ["owner", "environment"]

deny contains msg if {
    input.request.kind.kind == "Namespace"
    some label in required_labels
    not input.request.object.metadata.labels[label]
    msg := sprintf(
        "Namespace '%s' is missing required label '%s'",
        [input.request.object.metadata.name, label],
    )
}
