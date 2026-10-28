"""Cloud persistence, force-replan boundaries and supported wire modes."""
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

if "--source" in sys.argv:
    index = sys.argv.index("--source")
    sys.path.insert(0, sys.argv[index + 1])
    del sys.argv[index:index + 2]
import test_optimization as opt
fx = opt.fx
cf = opt.config_flow


class CloudDataReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_cloud_reconfigure_preserves_all_fields_and_clears_pending(self):
        for devices in ([{"mac": "AA", "mesh_id": 1, "name": "Desk"}], [{"mac": "AA", "mesh_id": 1}, {"mac": "BB", "mesh_id": 2}]):
            imported = opt.cloud.AwoxAccountImport("owner", "mesh", "secret", devices, len(devices))
            entry = types.SimpleNamespace(entry_id="entry", data={cf.CONF_SETUP_METHOD: cf.SETUP_METHOD_CLOUD, cf.CONF_DEVICES: []})
            flow = cf.AwoxConnectZConfigFlow()
            flow.hass = object()
            flow._get_reconfigure_entry = lambda: entry
            flow._configured_entries = lambda: [entry]
            flow.async_set_unique_id = AsyncMock()
            flow._abort_if_unique_id_mismatch = Mock()
            updates = []
            def save(item, **kwargs):
                self.assertIs(item, entry)
                updates.append(kwargs["data_updates"])
                self.assertIsNone(flow._pending_cloud_data)
                self.assertIsNone(flow._pending_cloud_review_counts)
                return {"type": "saved"}
            flow.async_update_reload_and_abort = save
            with patch.object(cf, "async_import_account", AsyncMock(return_value=imported)):
                result = await flow.async_step_reconfigure({cf.CONF_EMAIL: " User@Example.org ", cf.CONF_PASSWORD: "login-secret"})
            self.assertEqual(result, {"type": "saved"})
            self.assertEqual(updates, [{cf.CONF_SETUP_METHOD: cf.SETUP_METHOD_CLOUD, cf.CONF_EMAIL: "user@example.org", cf.CONF_OWNER_ID: "owner", cf.CONF_MESH_NAME: "mesh", cf.CONF_MESH_PASSWORD: "secret", cf.CONF_DEVICES: devices}])
            self.assertNotIn(cf.CONF_PASSWORD, updates[0])
            self.assertIsNot(updates[0][cf.CONF_DEVICES], devices)
            updates[0][cf.CONF_DEVICES][0]["name"] = "Updated"
            self.assertNotEqual(devices[0].get("name"), "Updated")
            self.assertEqual(entry.data[cf.CONF_DEVICES], [])
            flow._abort_if_unique_id_mismatch.assert_called_once_with(reason="wrong_account")

    def test_force_replan_defers_until_current_write_finishes(self):
        manager = fx.CommandManager(fx.S(True, True), minimum_interval_ms=0)
        manager.submit(fx.T(brightness=80))
        manager.take_next_command()
        before = tuple(manager._planned)
        self.assertEqual(manager.force_replan(), before)
        self.assertTrue(manager._replan_after_in_flight)
        self.assertEqual(manager.in_flight.kind, fx.K.BRIGHTNESS_ON)
        manager.mark_write_succeeded()
        self.assertEqual([c.kind for c in manager.force_replan()], [fx.K.POWER_ON])
        self.assertTrue(manager.power_finalizer_pending)
        manager.clear_pending()
        self.assertEqual(manager.force_replan(), ())

    def test_wire_mode_validation_keeps_unknown_and_static_ff_rejected(self):
        supported = set(range(8)) | {16, 17, 18, 19}
        for outer in (0x80, 0x82):
            for mode in range(256):
                for hue in (0, 255):
                    packet = fx.payload({"mode": mode, "hue": hue}, outer_type=outer)
                    accepted = mode in supported and not (mode in (2, 3) and hue == 255)
                    if accepted:
                        parsed = fx.p.parse_status_1911(fx.KEY, packet)
                        self.assertEqual(parsed["mode"], mode)
                        self.assertEqual(parsed["outer_type"], outer)
                    else:
                        with self.assertRaises(ValueError):
                            fx.p.parse_status_1911(fx.KEY, packet)


class CloudReviewCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_mesh_id_conversion_preserves_integer_string_and_error_cases(self):
        def previous(value):
            text = '' if value is None else str(value).strip()
            if text == '':
                return 0
            if isinstance(value, int):
                return value & 0xFFFF
            return int(text, 0 if text.lower().startswith('0x') else 10) & 0xFFFF
        values = list(range(-32768, 32768)) + [
            None, True, False, '', ' ', '0', ' -1 ', '0xFFFF', '0X01',
            '-0x01', '1.5', 1.5, [], {}, 'invalid', 2**100, -(2**100),
        ]
        for value in values:
            try:
                expected = previous(value)
            except (TypeError, ValueError) as error:
                with self.assertRaises(type(error)) as result:
                    opt.cloud._parse_mesh_id(value)
                self.assertEqual(str(result.exception), str(error))
            else:
                self.assertEqual(opt.cloud._parse_mesh_id(value), expected)

    async def test_review_save_matrix_preserves_counts_and_separate_save_paths(self):
        import itertools
        for reconfigure, imported_count, conflicts_count, previous, submitted in itertools.product(
                (False, True), (0, 1, 2), (0, 1, 2), ('none', 'same', 'changed'), (False, True)):
            with self.subTest(reconfigure=reconfigure, imported=imported_count,
                              conflicts=conflicts_count, previous=previous, submitted=submitted):
                flow = cf.AwoxConnectZConfigFlow()
                devices = [{'mac': str(i)} for i in range(imported_count)]
                conflicts = [{'mac': str(i)} for i in range(conflicts_count)]
                counts = (imported_count, conflicts_count)
                flow._pending_cloud_data = {cf.CONF_DEVICES: devices, 'preserved': 'value'}
                flow._pending_cloud_review_counts = (
                    None if previous == 'none' else counts if previous == 'same'
                    else (imported_count + 1, conflicts_count))
                flow._cloud_review_devices = Mock(return_value=(devices, conflicts))
                flow._get_reconfigure_entry = Mock(return_value=object())
                flow._abort_if_unique_id_configured = Mock()
                flow.async_create_entry = Mock(return_value={'type': 'create'})
                flow.async_update_reload_and_abort = Mock(return_value={'type': 'update'})
                step = flow.async_step_reconfigure_cloud_review if reconfigure else flow.async_step_cloud_review
                result = await step({} if submitted else None)
                saved = submitted and imported_count > 0 and previous != 'changed'
                self.assertEqual(result['type'], ('update' if reconfigure else 'create') if saved else 'form')
                if saved:
                    self.assertIsNone(flow._pending_cloud_data)
                    self.assertIsNone(flow._pending_cloud_review_counts)
                else:
                    self.assertIsNotNone(flow._pending_cloud_data)
                    self.assertEqual(flow._pending_cloud_review_counts, counts)
                    self.assertEqual(result['description_placeholders'], {
                        'import_count': str(imported_count), 'skipped_count': str(conflicts_count)})
                self.assertEqual(flow.async_create_entry.call_count, int(saved and not reconfigure))
                self.assertEqual(flow.async_update_reload_and_abort.call_count, int(saved and reconfigure))
                if submitted and imported_count > 0 and previous == 'changed':
                    result = await step({})
                    self.assertEqual(result['type'], 'update' if reconfigure else 'create')

    async def test_missing_review_data_still_aborts(self):
        flow = cf.AwoxConnectZConfigFlow()
        for step in (flow.async_step_cloud_review, flow.async_step_reconfigure_cloud_review):
            self.assertEqual(await step({}), {'type': 'abort', 'reason': 'local_setup_incomplete'})

    async def test_discovery_sets_compute_each_address_once(self):
        devices = [{'mac': 'aa', 'mesh_id': 1}, {'mac': 'BB', 'mesh_id': 2}, {'mac': '', 'mesh_id': 3}]
        for mode in ('new', 'extend', 'reconfigure'):
            with self.subTest(mode=mode):
                flow = cf.AwoxConnectZConfigFlow()
                flow._local_mesh_name, flow._local_mesh_password = 'mesh', 'password'
                entry = types.SimpleNamespace(entry_id='entry', data={cf.CONF_DEVICES: []})
                flow._matching_local_mesh_entry = Mock(return_value=None if mode == 'new' else entry)
                flow.async_set_unique_id = AsyncMock()
                flow._abort_if_unique_id_configured = Mock()
                flow._first_device_conflict = Mock(return_value=None)
                flow._merge_devices = Mock(return_value=devices)
                flow._async_abort_bluetooth_discovery_flows = Mock()
                flow.async_create_entry = Mock(return_value={'type': 'create'})
                flow.async_update_reload_and_abort = Mock(return_value={'type': 'update'})
                flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
                    async_update_entry=Mock(), async_reload=AsyncMock()))
                address = flow._device_address
                flow._device_address = Mock(wraps=address)
                if mode == 'reconfigure':
                    flow._get_reconfigure_entry = Mock(return_value=entry)
                    flow._local_scan_done = True
                    flow._local_candidates = {'AA': {}, 'BB': {}}
                    flow._async_verify_local_selection = AsyncMock(return_value=(devices, None, ''))
                    await flow.async_step_reconfigure_local_devices({cf.CONF_DEVICES: ['AA', 'BB']})
                else:
                    await flow._async_create_local_entry(devices)
                flow._async_abort_bluetooth_discovery_flows.assert_called_once_with({'AA', 'BB'})
                # Extend first checks whether each incoming record is new, then builds its set.
                self.assertEqual(flow._device_address.call_count, len(devices) * (2 if mode == 'extend' else 1))

    async def test_cloud_fallback_reuses_normalized_login_name(self):
        class Email(str):
            calls = 0
            def strip(self):
                self.calls += 1
                return super().strip()
        email = Email(' User@Example.org ')
        requests = []
        async def post(*args, **kwargs):
            requests.append(kwargs['payload'])
            if len(requests) == 1:
                raise opt.cloud.AwoxCloudError('transport failure')
            return 200, {'objectId': 'owner', 'sessionToken': 'token'}
        async def fetch(*args, **kwargs):
            if args[5] == 'Credential':
                return [{'service': 'zigbee', 'client_id': 'mesh', 'access_token': 'secret'}]
            return [{'type': '.ble.light', 'macAddress': 'AA:BB:CC:DD:EE:FF', 'address': 100}]
        with patch.object(opt.cloud, '_post_json', post), patch.object(opt.cloud, '_fetch_class', fetch):
            await opt.cloud.async_import_account(None, email, 'login-secret')
        self.assertEqual(email.calls, 1)
        self.assertEqual(len(requests), 2)
        self.assertEqual([item['username'] for item in requests], ['user@example.org'] * 2)
        self.assertEqual([item['password'] for item in requests], ['login-secret'] * 2)


if __name__ == "__main__":
    unittest.main()
