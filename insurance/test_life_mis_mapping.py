"""The Life Mapping screens (MIS Payout Engine): who may open them, the plan mapping and insurer alias forms, and the lists of
what the engine could not place."""
from django.contrib.auth.models import Group, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from insurance.models import (
    AuditLog, LifeGridUpload, LifeInsurerAlias, LifePlanMapping, LifeRate, MISFailedRow, MISFile,
)
from insurance.services import life_mis

GROUP = "Can_View_MIS_Payout_Engine"

PLAN_REASON = ("Failed on: Policy: product name / plan name — Plan '{plan}' is not mapped to any '{insurer}' Life grid "
               "product — add it on Life Plan Mapping.")
INSURER_REASON = "Failed on: Policy: insurance company — Insurer '{name}' is not a known Life insurer — add it on Life Insurer Aliases."


class LifeMappingBase(TestCase):
    def setUp(self):
        self.group = Group.objects.get_or_create(name=GROUP)[0]
        self.user = User.objects.create_user("life_mapper", password="unused-throwaway-1")
        self.user.groups.add(self.group)
        self.client.force_login(self.user)

        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        for insurer, product in [("HDFC Life", "Sanchay Plus"), ("HDFC Life", "Click 2 Achieve"),
                                 ("Tata AIA Life", "SRP 2 Pay - Life Option"), ("Tata AIA Life", "SRP 3 Pay - Life Option"),
                                 ("Tata AIA Life", "Fortune Guarantee Secure")]:
            LifeRate.objects.create(upload=self.upload, insurer=insurer, product=product, ppt="5", payout_yr1=10.0)

    def failed(self, reason, mis=None, row_id=None):
        mis = mis or MISFile.objects.create(uploaded_file="mis_uploads/x.csv")
        MISFailedRow.objects.create(
            mis_file=mis, row_id=row_id if row_id is not None else MISFailedRow.objects.count() + 1,
            status_key="NO_MATCH", mapping_status="❌ NO MATCH", failure_reason=reason)
        return mis

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class AccessTests(LifeMappingBase):
    names = ("life_plan_mapping", "life_insurer_aliases")

    def test_the_mis_payout_engine_group_opens_both_pages(self):
        for name in self.names:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_admin_opens_them_too(self):
        admin = User.objects.create_user("life_admin", password="unused-throwaway-1")
        admin.groups.add(Group.objects.get_or_create(name="ADMIN")[0])
        self.client.force_login(admin)
        for name in self.names:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_a_user_without_the_group_is_refused(self):
        other = User.objects.create_user("life_other", password="unused-throwaway-1")
        self.client.force_login(other)
        for name in self.names:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.post(reverse("life_plan_mapping_save"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("life_insurer_alias_save"), {}).status_code, 403)

    def test_anonymous_visitors_are_sent_to_login(self):
        self.client.logout()
        for name in self.names:
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_the_save_and_delete_routes_take_post_only(self):
        mapping = LifePlanMapping.objects.create(insurer="HDFC Life", plan_name="P", plan_key="p", grid_products="Sanchay Plus")
        alias = LifeInsurerAlias.objects.create(mis_name="Zed Life", name_key="zed life", insurer="HDFC Life")
        for url in (reverse("life_plan_mapping_save"), reverse("life_plan_mapping_delete", args=[mapping.id]),
                    reverse("life_insurer_alias_save"), reverse("life_insurer_alias_delete", args=[alias.id])):
            self.assertEqual(self.client.get(url).status_code, 405, url)

    def test_the_sidebar_links_to_the_screens_for_the_group(self):
        self.assertContains(self.client.get(reverse("life_plan_mapping")), 'href="/life-mis-mapping/"')

    def test_the_mis_payout_engine_page_has_a_button_to_the_screens(self):
        response = self.client.get(reverse("mis_payout_automation"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "🗂️ Life Mapping")
        self.assertContains(response, 'href="/life-mis-mapping/"')


class PlanMappingTests(LifeMappingBase):
    def post(self, **data):
        payload = {"insurer": "HDFC Life", "plan_name": "HDFC Life Smart Plan", "grid_products": "Sanchay Plus", "note": ""}
        payload.update(data)
        return self.client.post(reverse("life_plan_mapping_save"), payload, follow=True)

    def test_a_mapping_is_saved_with_its_folded_key_and_audited(self):
        response = self.post(grid_products="Sanchay Plus\n\n  Click 2 Achieve  \nSanchay Plus", note="from Sept MIS")
        mapping = LifePlanMapping.objects.get()
        self.assertEqual((mapping.insurer, mapping.plan_name, mapping.plan_key, mapping.note),
                         ("HDFC Life", "HDFC Life Smart Plan", "hdfc life smart plan", "from Sept MIS"))
        self.assertEqual(mapping.grid_products, "Sanchay Plus\nClick 2 Achieve")
        self.assertEqual(mapping.created_by, self.user)
        self.assertIn("now reaches 2 grid product(s), 2 row(s)", self.messages(response)[0])
        log = AuditLog.objects.get(action="LIFE PLAN MAPPING CREATE")
        self.assertIn("HDFC Life", log.details)
        self.assertIn("Sanchay Plus; Click 2 Achieve", log.details)

    def test_a_star_line_takes_every_product_starting_with_the_text(self):
        response = self.post(insurer="Tata AIA Life", plan_name="Tata AIA Sampoorna Raksha Promise", grid_products="SRP*")
        self.assertEqual(LifePlanMapping.objects.get().grid_products, "SRP*")
        self.assertIn("now reaches 2 grid product(s), 2 row(s)", self.messages(response)[0])

    def test_the_mapping_works_in_the_engines_matcher(self):
        self.post(insurer="Tata AIA Life", plan_name="Tata AIA Sampoorna Raksha Promise", grid_products="SRP*")
        grid = life_mis.load_life_grid()
        rows, how, stage, detail = life_mis.resolve_plan(
            "Tata AIA Sampoorna Raksha Promise", "Tata AIA Life", "TATA AIA Life Insurance Co. Ltd.", grid)
        self.assertEqual((how, stage, detail), ("mapping", "", None))
        self.assertEqual(sorted(row.product for row in rows), ["SRP 2 Pay - Life Option", "SRP 3 Pay - Life Option"])

    def test_a_product_the_grid_does_not_list_is_refused(self):
        response = self.post(grid_products="Sanchay Plus\nSanchai Plus")
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "&#x27;Sanchai Plus&#x27; is not a product of this insurer in the live Life grid")

    def test_a_product_of_another_insurer_is_refused(self):
        response = self.post(grid_products="SRP 2 Pay - Life Option")
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "is not a product of this insurer")

    def test_a_star_line_that_matches_nothing_is_refused(self):
        response = self.post(grid_products="Nothing Starts Like This*")
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "No product of this insurer starts with")

    def test_a_bare_star_is_refused(self):
        response = self.post(grid_products="*")
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "only * would take every product")

    def test_blank_fields_are_refused(self):
        self.post(plan_name="  ", grid_products="")
        self.assertFalse(LifePlanMapping.objects.exists())
        response = self.post(insurer="")
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "required")

    def test_an_insurer_outside_the_live_grid_is_refused(self):
        self.post(insurer="Kotak Mahindra Life")
        self.assertFalse(LifePlanMapping.objects.exists())

    def test_the_same_plan_cannot_be_mapped_twice_whatever_its_spelling(self):
        self.post()
        response = self.post(plan_name="hdfc  LIFE smart-plan")
        self.assertEqual(LifePlanMapping.objects.count(), 1)
        self.assertContains(response, "already mapped for this insurer")

    def test_the_same_plan_may_be_mapped_for_another_insurer(self):
        self.post()
        self.post(insurer="Tata AIA Life", grid_products="SRP*")
        self.assertEqual(LifePlanMapping.objects.count(), 2)

    def test_editing_changes_the_mapping_and_audits_what_it_was(self):
        self.post()
        mapping = LifePlanMapping.objects.get()
        self.post(id=mapping.id, grid_products="Click 2 Achieve", plan_name="HDFC Life Smart Plan (new)")
        mapping.refresh_from_db()
        self.assertEqual((mapping.plan_name, mapping.plan_key, mapping.grid_products),
                         ("HDFC Life Smart Plan (new)", "hdfc life smart plan new", "Click 2 Achieve"))
        self.assertEqual(LifePlanMapping.objects.count(), 1)
        log = AuditLog.objects.get(action="LIFE PLAN MAPPING UPDATE")
        self.assertIn("Was: HDFC Life / 'HDFC Life Smart Plan' -> Sanchay Plus", log.details)

    def test_editing_a_mapping_that_is_gone_says_so(self):
        response = self.post(id=9999)
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertIn("That mapping no longer exists.", self.messages(response))

    def test_deleting_removes_it_and_is_audited(self):
        self.post()
        mapping = LifePlanMapping.objects.get()
        response = self.client.post(reverse("life_plan_mapping_delete", args=[mapping.id]), follow=True)
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="LIFE PLAN MAPPING DELETE", details__contains="Smart Plan").exists())
        self.assertIn("Deleted the mapping", self.messages(response)[0])

    def test_the_edit_link_fills_the_form(self):
        self.post(note="a note")
        mapping = LifePlanMapping.objects.get()
        response = self.client.get(reverse("life_plan_mapping") + f"?edit={mapping.id}")
        self.assertContains(response, "Edit a mapping")
        self.assertContains(response, "HDFC Life Smart Plan")
        self.assertContains(response, f'name="id" value="{mapping.id}"')

    def test_the_map_link_of_a_waiting_plan_fills_the_form(self):
        response = self.client.get(reverse("life_plan_mapping") + "?insurer=Tata+AIA+Life&plan=Tata+AIA+Promise")
        form = response.context["form"]
        self.assertEqual(form.initial, {"insurer": "Tata AIA Life", "plan_name": "Tata AIA Promise"})

    def test_a_bad_edit_id_is_ignored(self):
        self.assertEqual(self.client.get(reverse("life_plan_mapping") + "?edit=abc").status_code, 200)
        self.assertEqual(self.client.get(reverse("life_plan_mapping") + "?edit=99999").status_code, 200)

    def test_the_picker_gets_every_insurers_products(self):
        response = self.client.get(reverse("life_plan_mapping"))
        self.assertEqual(response.context["picker_products"]["HDFC Life"], ["Click 2 Achieve", "Sanchay Plus"])
        self.assertContains(response, 'id="lifeProductsData"')

    def test_the_list_shows_what_each_mapping_reaches_and_flags_a_stale_one(self):
        LifePlanMapping.objects.create(insurer="HDFC Life", plan_name="Good", plan_key="good", grid_products="Sanchay Plus")
        LifePlanMapping.objects.create(insurer="HDFC Life", plan_name="Stale", plan_key="stale", grid_products="Gone Plan")
        response = self.client.get(reverse("life_plan_mapping"))
        self.assertContains(response, "1 product, 1 row")
        self.assertContains(response, "Nothing in the live grid")

    def test_the_list_can_be_filtered(self):
        LifePlanMapping.objects.create(insurer="HDFC Life", plan_name="Alpha", plan_key="alpha", grid_products="Sanchay Plus")
        LifePlanMapping.objects.create(insurer="Tata AIA Life", plan_name="Beta", plan_key="beta", grid_products="SRP*")
        by_insurer = self.client.get(reverse("life_plan_mapping") + "?insurer=Tata+AIA+Life")
        self.assertEqual([m["mapping"].plan_name for m in by_insurer.context["mappings"]], ["Beta"])
        by_text = self.client.get(reverse("life_plan_mapping") + "?q=sanchay")
        self.assertEqual([m["mapping"].plan_name for m in by_text.context["mappings"]], ["Alpha"])

    def test_with_no_live_grid_the_page_says_so_and_saving_is_refused(self):
        self.upload.is_active = False
        self.upload.save()
        page = self.client.get(reverse("life_plan_mapping"))
        self.assertContains(page, "No Life grid is live")
        response = self.post()
        self.assertFalse(LifePlanMapping.objects.exists())
        self.assertContains(response, "No Life grid is live, so there is nothing to map to yet")


class WaitingListTests(LifeMappingBase):
    def test_plans_the_engine_could_not_place_are_listed_most_policies_first_with_hints(self):
        mis = self.failed(PLAN_REASON.format(plan="Sanchay Plus Gold", insurer="HDFC Life"))
        self.failed(PLAN_REASON.format(plan="Tata AIA Promise", insurer="Tata AIA Life"), mis)
        self.failed(PLAN_REASON.format(plan="Tata AIA Promise", insurer="Tata AIA Life"), mis)
        response = self.client.get(reverse("life_plan_mapping"))
        waiting = response.context["waiting"]
        self.assertEqual([(w["insurer"], w["plan"], w["count"]) for w in waiting],
                         [("Tata AIA Life", "Tata AIA Promise", 2), ("HDFC Life", "Sanchay Plus Gold", 1)])
        self.assertEqual(waiting[1]["close"], ["Sanchay Plus"])
        self.assertContains(response, "Plans waiting to be mapped")
        self.assertContains(response, "insurer=HDFC%20Life&amp;plan=Sanchay%20Plus%20Gold")

    def test_a_mapped_plan_leaves_the_list(self):
        self.failed(PLAN_REASON.format(plan="Sanchay Plus Gold", insurer="HDFC Life"))
        LifePlanMapping.objects.create(insurer="HDFC Life", plan_name="sanchay plus  GOLD", plan_key="sanchay plus gold",
                                       grid_products="Sanchay Plus")
        self.assertEqual(self.client.get(reverse("life_plan_mapping")).context["waiting"], [])

    def test_nothing_waiting_shows_no_waiting_card(self):
        response = self.client.get(reverse("life_plan_mapping"))
        self.assertEqual(response.context["waiting"], [])
        self.assertNotContains(response, "bi-hourglass-split")      # the waiting card's icon

    def test_insurer_names_are_listed_on_their_own_page(self):
        self.failed(INSURER_REASON.format(name="Max Life Insurance Co. Ltd."))
        self.failed(INSURER_REASON.format(name="Max Life Insurance Co. Ltd."))
        self.failed("Failed on: Policy: insurance company — Insurer 'Bajaj Allianz General Insurance Company Limited' is a "
                    "General insurer's name on a Life policy — correct the MIS, or add it on Life Insurer Aliases if it is "
                    "the Life company.")
        response = self.client.get(reverse("life_insurer_aliases"))
        self.assertEqual([(w["name"], w["count"]) for w in response.context["waiting"]],
                         [("Max Life Insurance Co. Ltd.", 2), ("Bajaj Allianz General Insurance Company Limited", 1)])
        self.assertContains(response, "name=Max%20Life%20Insurance%20Co.%20Ltd.")


class InsurerAliasTests(LifeMappingBase):
    def post(self, **data):
        payload = {"mis_name": "Max Life Insurance Co. Ltd.", "insurer": "HDFC Life"}
        payload.update(data)
        return self.client.post(reverse("life_insurer_alias_save"), payload, follow=True)

    def test_an_alias_is_saved_with_its_folded_key_and_audited(self):
        response = self.post()
        alias = LifeInsurerAlias.objects.get(insurer="HDFC Life", mis_name="Max Life Insurance Co. Ltd.")
        self.assertEqual(alias.name_key, "max life insurance co ltd")
        self.assertEqual(alias.created_by, self.user)
        self.assertIn("now means HDFC Life", self.messages(response)[0])
        self.assertTrue(AuditLog.objects.filter(action="LIFE INSURER ALIAS CREATE", details__contains="Max Life").exists())

    def test_the_engines_matcher_uses_it(self):
        self.post()
        insurer, detail = life_mis.resolve_insurer("MAX LIFE insurance co ltd", life_mis.load_life_grid())
        self.assertEqual((insurer, detail), ("HDFC Life", None))

    def test_the_same_name_cannot_be_mapped_twice_whatever_its_spelling(self):
        self.post()
        response = self.post(mis_name="max-life  INSURANCE co. ltd", insurer="Tata AIA Life")
        self.assertEqual(LifeInsurerAlias.objects.filter(name_key="max life insurance co ltd").count(), 1)
        self.assertContains(response, "This name is already mapped")

    def test_an_insurer_outside_the_live_grid_is_refused(self):
        self.post(insurer="Kotak Mahindra Life")
        self.assertFalse(LifeInsurerAlias.objects.filter(mis_name="Max Life Insurance Co. Ltd.").exists())

    def test_editing_and_deleting(self):
        self.post()
        alias = LifeInsurerAlias.objects.get(mis_name="Max Life Insurance Co. Ltd.")
        self.post(id=alias.id, insurer="Tata AIA Life")
        alias.refresh_from_db()
        self.assertEqual(alias.insurer, "Tata AIA Life")
        self.assertIn("Was: 'Max Life Insurance Co. Ltd.' -> HDFC Life",
                      AuditLog.objects.get(action="LIFE INSURER ALIAS UPDATE").details)
        response = self.client.post(reverse("life_insurer_alias_delete", args=[alias.id]), follow=True)
        self.assertFalse(LifeInsurerAlias.objects.filter(pk=alias.id).exists())
        self.assertTrue(AuditLog.objects.filter(action="LIFE INSURER ALIAS DELETE").exists())
        self.assertIn("Deleted the alias", self.messages(response)[0])

    def test_the_edit_link_fills_the_form(self):
        self.post()
        alias = LifeInsurerAlias.objects.get(mis_name="Max Life Insurance Co. Ltd.")
        response = self.client.get(reverse("life_insurer_aliases") + f"?edit={alias.id}")
        self.assertContains(response, "Edit an insurer name")
        self.assertEqual(response.context["form"].initial["mis_name"], "Max Life Insurance Co. Ltd.")

    def test_the_list_flags_an_alias_pointing_at_an_insurer_the_grid_no_longer_has(self):
        LifeInsurerAlias.objects.create(mis_name="Old Name", name_key="old name", insurer="Retired Life")
        self.assertContains(self.client.get(reverse("life_insurer_aliases")), "Not an insurer of the live grid")

    def test_the_seeded_aliases_are_listed(self):
        response = self.client.get(reverse("life_insurer_aliases"))
        self.assertContains(response, "India First Life Insurance Co.Ltd.")
