package kubernetes.admission.container_limits

import rego.v1

deny contains msg if {
    input.request.kind.kind == "Pod"
    some container in input.request.object.spec.containers
    not container.resources.limits.cpu
    msg := sprintf("Container '%s' must have CPU limits set", [container.name])
}

deny contains msg if {
    input.request.kind.kind == "Pod"
    some container in input.request.object.spec.containers
    not container.resources.limits.memory
    msg := sprintf("Container '%s' must have memory limits set", [container.name])
}

deny contains msg if {
    input.request.kind.kind == "Pod"
    some container in input.request.object.spec.initContainers
    not container.resources.limits.cpu
    msg := sprintf("Init container '%s' must have CPU limits set", [container.name])
}

deny contains msg if {
    input.request.kind.kind == "Pod"
    some container in input.request.object.spec.initContainers
    not container.resources.limits.memory
    msg := sprintf("Init container '%s' must have memory limits set", [container.name])
}
