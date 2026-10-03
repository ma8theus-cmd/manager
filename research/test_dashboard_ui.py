import unittest
from pathlib import Path


class DashboardUiTests(unittest.TestCase):
    def test_promising_members_section_starts_collapsed(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('<details class="compact-section" id="promising-members">', template)
        self.assertNotIn('<details class="compact-section" id="promising-members" open>', template)



    def test_assignment_shows_confirmation_controls_immediately(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn("statusEl.textContent = 'AGUARDANDO CONFIRMAÇÃO';", template)
        self.assertIn("form.className = 'comment-confirm-form';", template)
        self.assertIn("button.textContent = 'Confirmar publicação';", template)



    def test_submitted_comment_hides_confirmation_and_shows_scout_status(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('ENVIADO · SCOUT ATIVO', template)
        self.assertIn("and not c.confirmed_at and not c.submitted_at", template)


    def test_assignment_serializes_member_before_disabling_select(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        form_data = template.index('const body=new URLSearchParams(new FormData(form));')
        disable_select = template.index('select.disabled=true;')
        self.assertLess(
            form_data,
            disable_select,
            'o member_id deve ser serializado antes de desabilitar o select',
        )

    def test_assignment_ui_respects_automatic_delegated_mode(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('if (data.requires_confirmation)', template)
        self.assertIn('NA FILA PARA ENVIO', template)

    def test_comment_cadence_is_visible_in_the_panel(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('1 a cada {{ data.comments.professional_interval_hours }} horas por profissional', template)
        self.assertIn('professional_interval_hours', template)
        self.assertIn('minimum_interval_minutes', template)


    def test_account_creation_has_independent_manual_route_buttons(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('action="/prepare-batch/direct"', template)
        self.assertIn('action="/prepare-batch/vpn"', template)
        self.assertIn('Preparar lote de 5 contas direto', template)
        self.assertIn('Preparar lote de 5 contas com VPN', template)
        self.assertNotIn('id="batch-countdown"', template)
        self.assertNotIn('Preparar lote diário', template)
        self.assertNotIn('{{ data.interval_hours }}h', template)


    def test_backend_exposes_route_specific_batch_endpoint(self):
        source = Path('/home/matheus/meslibertines_manager_v1/app/main.py').read_text()
        self.assertIn('@app.post("/prepare-batch/{route}")', source)
        self.assertIn('reserve_batch(route)', source)

    def test_ajax_member_actions_return_json_without_redirect(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import main

        request = SimpleNamespace(headers={"x-requested-with": "XMLHttpRequest"})
        with patch.object(main, "mark_email_confirmed"), patch.object(
            main, "launch_profile_activation", return_value=1234
        ):
            email_response = main.email_confirmed(71, request)
        self.assertTrue(email_response["ok"])
        self.assertEqual(email_response["member_id"], 71)
        self.assertEqual(email_response["email_status"], "CONFIRMED")
        self.assertEqual(email_response["profile_status"], "ACTIVATING")

        with patch.object(
            main,
            "get_members_by_ids",
            return_value=[{"email_status": "CONFIRMED", "profile_status": "PENDING"}],
        ), patch.object(main, "launch_profile_activation", return_value=5678):
            profile_response = main.activate_profile(71, request)
        self.assertTrue(profile_response["ok"])
        self.assertEqual(profile_response["member_id"], 71)
        self.assertEqual(profile_response["profile_status"], "ACTIVATING")

    def test_non_ajax_member_actions_keep_redirect_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi.responses import RedirectResponse
        from app import main

        request = SimpleNamespace(headers={})
        with patch.object(main, "mark_email_confirmed"), patch.object(
            main, "launch_profile_activation", return_value=1234
        ):
            email_response = main.email_confirmed(71, request)
        self.assertIsInstance(email_response, RedirectResponse)
        self.assertEqual(email_response.status_code, 303)

        with patch.object(
            main,
            "get_members_by_ids",
            return_value=[{"email_status": "CONFIRMED", "profile_status": "PENDING"}],
        ), patch.object(main, "launch_profile_activation", return_value=5678):
            profile_response = main.activate_profile(71, request)
        self.assertIsInstance(profile_response, RedirectResponse)
        self.assertEqual(profile_response.status_code, 303)

    def test_member_actions_are_intercepted_without_page_reload(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('data-member-action="email-confirmed"', template)
        self.assertIn('data-member-action="activate-profile"', template)
        self.assertIn('event.preventDefault()', template)
        self.assertIn("X-Requested-With': 'XMLHttpRequest'", template)
        self.assertIn('fetch(form.action', template)

    def test_profile_activation_completion_replaces_button_in_place(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('data-member-id="{{ m.id }}"', template)
        self.assertIn('data-profile-activation-action', template)
        self.assertIn('member-profile-status', template)
        self.assertIn("profileStatus === 'COMPLETE'", template)
        self.assertIn('✓ Perfil completo', template)
        self.assertIn("const statusUrl = '/members/' + memberId + '/status';", template)

    def test_member_experience_batches_read_items_key_explicitly(self):
        template = Path('/home/matheus/meslibertines_manager_v1/app/templates/dashboard.html').read_text()
        self.assertIn('{% for item in batch["items"] %}', template)
        self.assertNotIn('{% for item in batch.items %}', template)


if __name__ == "__main__":
    unittest.main()
