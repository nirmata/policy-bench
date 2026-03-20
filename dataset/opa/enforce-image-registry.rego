package kubernetes.admission.enforce_image_registry

# Deny pods using images from untrusted registries.
# Only images from the approved registries list are allowed.

import rego.v1

approved_registries := [
    "gcr.io/mycompany/",
    "docker.io/library/",
    "registry.k8s.io/",
]

deny contains msg if {
    input.request.kind.kind == "Pod"
    some container in input.request.object.spec.containers
    not image_from_approved(container.image)
    msg := sprintf(
        "Container '%s' uses image '%s' from an untrusted registry. Approved registries: %v",
        [container.name, container.image, approved_registries],
    )
}

image_from_approved(image) if {
    some registry in approved_registries
    startswith(image, registry)
}
