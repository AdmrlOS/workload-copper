import unittest
from scripts.configure_admiral import build_configuration


class ConfigurationTests(unittest.TestCase):
    def test_readback_accepts_only_known_empty_field_serialization(self):
        import copy
        from scripts.configure_admiral import verify_readback
        payload = build_configuration('ghcr.io/admrlos/workload-copper@sha256:' + 'a' * 64)
        document = dict(metadata={"name": payload["name"]}, spec=copy.deepcopy(payload["spec"]),
                        status={"fleetUsage": []})
        # The API's Go BSON omitempty fields reload as null/omitted, not empty arrays.
        document["spec"]["command"] = None
        document["spec"]["mounts"] = None
        del document["spec"]["options"]["host_namespaces"]
        verify_readback(document, payload)
        document["spec"]["options"]["host_namespaces"] = ["network"]
        with self.assertRaises(RuntimeError):
            verify_readback(document, payload)

    def test_readback_rejects_every_requested_spec_drift_and_extra_device_access(self):
        import copy
        from scripts import configure_admiral as configure
        self.assertTrue(hasattr(configure, "verify_readback"), "full-spec read-back verification is missing")
        payload = build_configuration('ghcr.io/admrlos/workload-copper@sha256:' + 'a' * 64)
        baseline = dict(metadata={"name": payload["name"]}, spec=copy.deepcopy(payload["spec"]),
                        status={"fleetUsage": []})
        configure.verify_readback(baseline, payload)
        changes = [(["ports"], [{"port": 22, "protocol": "tcp"}]),
                   (["volumes", "data", "destination"], "/etc"),
                   (["volumes", "data", "options"], ["rbind", "ro"]),
                   (["environment", "COPPER_MAX_BYTES"], "999999999"),
                   (["mounts"], [{"source": "/dev", "destination": "/dev"}]),
                   (["devices"], ["/dev/mem"]),
                   (["command"], ["/bin/sh"]),
                   (["desiredState"], "STOPPED"),
                   (["options", "capabilities"], ["CAP_SYS_ADMIN"]),
                   (["options", "all_devices_allowed"], True),
                   (["options", "privileged"], True),
                   (["options", "new_privileges"], True)]
        for path, value in changes:
            with self.subTest(path=path):
                document = copy.deepcopy(baseline)
                target = document["spec"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaisesRegex(RuntimeError, "read-back.*spec"):
                    configure.verify_readback(document, payload)
        normalized = copy.deepcopy(baseline)
        normalized["spec"]["volumes"]["data"]["options"].reverse()
        configure.verify_readback(normalized, payload)
        assigned = copy.deepcopy(baseline)
        assigned["status"]["fleetUsage"] = ["unexpected-fleet"]
        with self.assertRaisesRegex(RuntimeError, "fleet usage"):
            configure.verify_readback(assigned, payload)

    def test_cpu_demo_has_no_host_hardware_and_uses_persistent_private_image(self):
        image = 'ghcr.io/admrlos/workload-copper@sha256:' + 'a' * 64
        spec = build_configuration(image)['spec']
        self.assertEqual(spec['image'], image)
        self.assertEqual(spec['registry_credential_id'], '8927588f-297a-424a-acae-6fc87e4a1de6')
        self.assertEqual(spec['volumes']['data']['destination'], '/data')
        self.assertEqual(spec['ports'], [{'port': 8080, 'protocol': 'tcp'}])
        self.assertFalse(spec['options']['all_devices_allowed'])
        self.assertFalse(spec['options']['privileged'])
        self.assertEqual(spec['options']['host_namespaces'], [])
        self.assertNotIn('fleet', str(spec))

    def test_only_exact_immutable_target_references_are_accepted(self):
        for image in ['ghcr.io/admrlos/workload-copper:latest',
                      'ghcr.io/admrlos/other@sha256:' + 'a' * 64,
                      'ghcr.io/admrlos/workload-copper@sha256:' + 'A' * 64]:
            with self.subTest(image=image), self.assertRaises(ValueError):
                build_configuration(image)
