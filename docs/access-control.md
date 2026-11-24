# Access control

Permissions are real Django groups, managed at `/user-management/`. Each page has one group (`Can_View_...`,
`Can_Upload_CSV`, ...); the `ADMIN` group opens every page. `/user-management/` itself is open to staff, superusers
and `ADMIN`.

## The gates (`insurance/access.py`)

| Gate | Lets in | Used for |
|---|---|---|
| `page_access_required("<group>")` | signed-in users in `ADMIN` or the named group. The `is_superuser` flag is **not** a bypass. | almost every page |
| `staff_required` | signed-in users with `is_staff`, `is_superuser` or in `ADMIN` | `/user-management/`, the API docs |
| `super_admin_required` | signed-in users in `SUPER_ADMIN` only (`ADMIN` is not enough) | nothing today; kept for the next super-admin-only action. Grant `SUPER_ADMIN` from Django admin, not from the checkboxes. |
| `login_required` | any signed-in user | home, the Rate Checker entry, raising a ticket |
| `HasAPIKey` (DRF) | a valid API key | the server-to-server API routes |

## Three places that must agree

When you change who can open a page, change all of these:

1. the wrapper on the route in `insurance/urls.py` (and `access.PAGE_GROUPS` for a new group);
2. `insurance/context_processors.py::sidebar_access`, which draws the sidebar links from `PAGE_GROUPS` and the user's
   groups (`SidebarMatchesPageGateTests` compares it with the decorators for every group);
3. a data migration that creates the group (see `insurance/migrations/0021_seed_page_access_groups.py`).

Group names are stable identifiers referenced by migrations and held by real users. Never rename one: it silently
revokes access for everyone already assigned.

`insurance/tests.py::UrlAuthGateTests` fails if a new named route is reachable without login; add a name to its
`PUBLIC_URL_NAMES` only for genuinely public pages. `ApiKeyGatedRouteTests` lists the API-key routes exactly.

## Who sees whose activity (Team Activity)

`/team-activity/` is the one page with a rule on rows as well as on the page. A user needs both layers:

1. **The page gate**: the group `Can_View_Team_Activity` (or `ADMIN`), like any other page.
2. **The row rule**: `insurance/services/team_scope.py::visible_events(user)`. `ADMIN` gets every event; everyone else
   gets the events of the people below them in the **Reports To** hierarchy (`UserProfile.reports_to`, set in Access
   Control under Teams Hierarchy): direct reports and theirs, recursively, never themselves, a peer or a boss. No
   profile, nobody reporting to you, or not signed in: nothing.

Every view and export starts from `visible_events()` and only narrows it, so a forged `?person=` or `?team=` cannot
reach another team (`?team=` is not even read for anyone but `ADMIN`). `test_team_activity.StructureTests` fails if a
view or the API queries `ActivityEvent` directly.

What the page shows per event has two parts. The **sentence** (`summary`, and the small `extra` counts) never carries
personal data. The **Details** (`ActivityEvent.details`) are the exact parameters of what the person did: the values a
check was run with (a vehicle number, a policy-holder name), what an edit changed from and to, the rows a bulk update
touched, the ticket text. Details can therefore hold personal data, and they sit under exactly the same row rule as the
sentence: a manager reads them only for the people below them, `ADMIN` for everyone. Never written into them: any
password (a new user's one-time password, a reset password), and the values of PAN, bank account, bank name and IFSC
(Access Control logs those as "changed" only).

Every audit-log entry also lands on this page, as an Edit event whose details are the entry's text (the entries written by
`audit_change` carry the exact parameters as well). That is what lets Team Activity replace the Security Audit page: the
page itself is still there with its own 7-day retention, while Team Activity keeps last month and this month.

Some events have **nobody signed in** behind them: a search made through the API with an API key (the Points Logs page
calls these "System"), an audit entry written by a management command. They are filed under a **System** pseudo-person.
No manager has System in their team, so only `ADMIN` reads these events; the Person filter's System choice, the System
bar and `?person=system` exist only for them and find nothing for anyone else. Every event also shows its **source path**:
the page or API address that caused it (`/api/v1/health-payout-rates/`, `/motor-payout-rates/`; never the query string,
which can hold a vehicle number), `celery:<task>` for a background job, `manage.py <command>` for a command, or "not
recorded" for events from before this was kept.

Reports To is a tree: one manager per person, `UserProfile.clean()` refuses self-links and loops, and every change is
written to the audit log as `TEAM CHANGE` (so it is on Team Activity too), because it decides who can read whose work. Run
`python manage.py check_team_hierarchy` to list the links and spot bad ones. Deleting a manager's account leaves their
reports without a manager (their events are then visible to admins only): deactivate the account instead.

## Route table

Generated by `python manage.py show_access_matrix` (read-only; it inspects the live URL table). Regenerate this section
after adding or re-gating a route.

| Route | Name | Who can open it |
|---|---|---|
| `/` | `home` | any signed-in user |
| `/<path:unused>/` | `catch_all` | public (redirects to the login page) |
| `/analysis/` | `business_analysis` | group `Can_View_Analysis` or ADMIN |
| `/api/check-duplicate-upload/` | `api_check_duplicate_upload` | group `Can_Upload_CSV` or ADMIN |
| `/api/cluster-details/` | `get_cluster_details` | group `Can_View_Dashboard` or ADMIN |
| `/api/cluster-details/update/` | `update_cluster_details` | group `Can_View_Dashboard` or ADMIN |
| `/api/complete-upload/` | `api_complete_upload` | group `Can_Upload_CSV` or ADMIN |
| `/api/create-ticket/` | `create_ticket_api` | any signed-in user |
| `/api/health-payout-rates/` | `api_health_payout_rates` | API key |
| `/api/make-model-master/` | `api_make_model_master` | API key |
| `/api/policy-lock-checker/` | `api_policy_lock_checker` | API key |
| `/api/schema/` | `schema` | staff, superuser or ADMIN |
| `/api/schema/redoc/` | `redoc` | staff, superuser or ADMIN |
| `/api/schema/swagger-ui/` | `swagger-ui` | staff, superuser or ADMIN |
| `/api/sso/issue-ticket/` | `sso_issue_ticket` | API key |
| `/api/update-special-rate-status/` | `update_special_rate_request_status` | group `Can_Review_Special_Rates` or ADMIN |
| `/api/update-ticket-status/` | `update_ticket_status` | group `Can_View_Tickets` or ADMIN |
| `/api/upload-chunk/` | `api_upload_chunk` | group `Can_Upload_CSV` or ADMIN |
| `/api/v1/export-rates/` | `api-export-rates` | group `Can_View_Dashboard` or ADMIN + API key |
| `/api/v1/health-payout-rates/` | `api_v1_health_payout_rates` | API key |
| `/api/v1/make-model-master/` | `api_v1_make_model_master` | API key |
| `/api/v1/policy-lock-checker/` | `api_v1_policy_lock_checker` | API key |
| `/api/v1/sso/issue-ticket/` | `api_v1_sso_issue_ticket` | API key |
| `/audit-log/` | `audit_logs` | group `Can_View_Audit_Log` or ADMIN |
| `/audit-log/export/` | `export_audit_log_xlsx` | group `Can_View_Audit_Log` or ADMIN |
| `/bulk-update/` | `bulk_update_rates` | group `Can_View_Dashboard` or ADMIN |
| `/configurator/` | `field_configurator` | group `Can_View_AI_Rulebook` or ADMIN |
| `/configurator/delete/<int:pk>/` | `delete_field` | group `Can_View_AI_Rulebook` or ADMIN |
| `/configurator/edit/<int:pk>/` | `edit_field` | group `Can_View_AI_Rulebook` or ADMIN |
| `/dashboard/` | `dashboard` | group `Can_View_Dashboard` or ADMIN |
| `/edit-rate/<str:group_id>/` | `edit_rate` | group `Can_View_Dashboard` or ADMIN |
| `/export-make-model/` | `export_make_model_xlsx` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/export-pincode/` | `export_pincode_xlsx` | group `Can_View_Pincode_Dashboard` or ADMIN |
| `/export-rto/` | `export_rto_xlsx` | group `Can_View_RTO_Dashboard` or ADMIN |
| `/export/` | `export_rates_xlsx` | group `Can_View_Dashboard` or ADMIN |
| `/grid-management/` | `grid_management` | group `Can_View_Grid_Management` or ADMIN |
| `/health-payout-rates/` | `health_payout_rates` | group `Can_View_Health_Payout_Rates` or ADMIN |
| `/health-rate-master/` | `health_rate_master` | group `Can_View_Health_Rate_Master` or ADMIN |
| `/health-rate-master/bulk-update/` | `health_rate_master_bulk_update` | group `Can_View_Health_Rate_Master` or ADMIN |
| `/health-rate-master/edit/<int:pk>/` | `health_rate_master_edit` | group `Can_View_Health_Rate_Master` or ADMIN |
| `/health-rate-master/export/` | `export_health_rates_xlsx` | group `Can_View_Health_Rate_Master` or ADMIN |
| `/home/` | `home_dashboard` | any signed-in user |
| `/life-mis-mapping/` | `life_plan_mapping` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-mis-mapping/delete/<int:pk>/` | `life_plan_mapping_delete` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-mis-mapping/insurers/` | `life_insurer_aliases` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-mis-mapping/insurers/delete/<int:pk>/` | `life_insurer_alias_delete` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-mis-mapping/insurers/save/` | `life_insurer_alias_save` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-mis-mapping/save/` | `life_plan_mapping_save` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/life-payout-rates/` | `life_payout_rates` | group `Can_View_Life_Payout_Grid` or ADMIN |
| `/life-rate-master/` | `life_rate_master` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/activate/<int:pk>/` | `life_rate_master_activate` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/add/` | `life_rate_master_add` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/bulk-update/` | `life_rate_master_bulk_update` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/dashboard/` | `life_rate_master_dashboard` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/dashboard/download/` | `life_rate_master_dashboard_download` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/delete/<int:pk>/` | `life_rate_master_delete` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/edit/<int:pk>/` | `life_rate_master_edit` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/export/` | `life_rate_master_export` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/notes/` | `life_rate_master_notes` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/notes/<int:pk>/edit/` | `life_rate_master_note_edit` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/notes/add/` | `life_rate_master_note_add` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/upload/` | `life_rate_master_upload` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/` | `life_rate_master_uploads` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/<int:pk>/delete/` | `life_rate_master_delete_upload` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/<int:pk>/discard/` | `life_rate_master_discard` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/<int:pk>/review/` | `life_rate_master_review` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/standard/` | `life_rate_master_upload_standard` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/life-rate-master/uploads/template/` | `life_rate_master_template` | group `Can_View_Life_Rate_Master` or ADMIN |
| `/lock-unlock-policy/<int:rate_id>/` | `lock_unlock_policy` | group `Can_View_Policy_Locker` or ADMIN |
| `/locked-policy-dashboard/` | `locked_policy_dashboard` | group `Can_View_Locked_Policies` or ADMIN |
| `/locked-policy-dashboard/export/` | `export_locked_policies_xlsx` | group `Can_View_Locked_Policies` or ADMIN |
| `/login/` | `login` | public |
| `/logout/` | `logout` | public |
| `/make-model-dashboard/` | `make_model_dashboard` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-dashboard/create/` | `create_make_model` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-dashboard/search/` | `make_model_dashboard_search` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/` | `make_model_mapping` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/delete/` | `delete_make_model_mapping` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/list/` | `make_model_mapping_list` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/save/` | `save_make_model_mapping` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/suggest/` | `make_model_mapping_suggest` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model-mapping/values/` | `make_model_mapping_values` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/make-model/edit/<int:pk>/` | `edit_make_model` | group `Can_View_Make_Model_Dashboard` or ADMIN |
| `/mis-mapping/` | `mis_mapping_dashboard` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-mapping/add/` | `add_mis_mapping` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-mapping/delete/<int:pk>/` | `delete_mis_mapping` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-mapping/edit/<int:pk>/` | `edit_mis_mapping` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-payout-automation/` | `mis_payout_automation` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-payout/cancel/<int:file_id>/` | `cancel_mis_processing` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-payout/download/<int:file_id>/` | `download_processed_mis` | group `Can_View_MIS_Payout_Engine` or ADMIN |
| `/mis-review/<int:pk>/` | `mis_review` | group `Can_View_MIS_Register` or ADMIN |
| `/missing-make-model/` | `missing_make_model` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/add-to-master/` | `add_missing_make_model_to_master` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/bulk-add-to-master/` | `bulk_add_missing_make_model_to_master` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/bulk-mark-resolved/` | `bulk_mark_missing_make_model_resolved` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/export/` | `export_missing_make_model_xlsx` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/mark-resolved/` | `mark_missing_make_model_resolved` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/missing-make-model/unmark-resolved/` | `unmark_missing_make_model_resolved` | group `Can_View_Missing_Make_Model` or ADMIN |
| `/motor-payout-rates/` | `motor_payout_rates` | group `Can_View_Motor_Payout_Rates` or ADMIN |
| `/motor-payout-rates/more/` | `motor_payout_rates_more` | group `Can_View_Motor_Payout_Rates` or ADMIN |
| `/my-mis/` | `my_mis` | group `Can_View_MIS_Register` or ADMIN |
| `/password-reset/` | `password_reset` | public |
| `/password-reset/done/` | `password_reset_done` | public |
| `/pincode-dashboard/` | `pincode_dashboard` | group `Can_View_Pincode_Dashboard` or ADMIN |
| `/pincode-dashboard/create/` | `create_pincode` | group `Can_View_Pincode_Dashboard` or ADMIN |
| `/pincode-dashboard/search/` | `pincode_dashboard_search` | group `Can_View_Pincode_Dashboard` or ADMIN |
| `/pincode/edit/<int:pk>/` | `edit_pincode` | group `Can_View_Pincode_Dashboard` or ADMIN |
| `/points-logs/` | `points_audit_logs` | group `Can_View_Motor_Points_Logs` or ADMIN |
| `/points-logs/export/` | `export_points_audit_logs_xlsx` | group `Can_View_Motor_Points_Logs` or ADMIN |
| `/policy-lock-checker/` | `policy_lock_checker` | group `Can_View_Policy_Locker` or ADMIN |
| `/rate-checker/` | `rate_checker` | any signed-in user |
| `/rate-master-health/` | `rate_master_health` | group `Can_View_Rate_Master_Health` or ADMIN |
| `/rate-master-health/deactivate-group/<int:group_key>/` | `deactivate_rate_group` | group `Can_View_Rate_Master_Health` or ADMIN |
| `/rate-master-health/export-grid-summary/` | `export_grid_summary_xlsx` | group `Can_View_Rate_Master_Health` or ADMIN |
| `/rate-master-health/export-overlap-pairs/` | `export_overlap_pairs_xlsx` | group `Can_View_Rate_Master_Health` or ADMIN |
| `/rate-master-health/overlap-scan/` | `start_overlap_scan` | group `Can_View_Rate_Master_Health` or ADMIN |
| `/reset/<uidb64>/<token>/` | `password_reset_confirm` | public |
| `/reset/done/` | `password_reset_complete` | public |
| `/rto-dashboard/` | `rto_dashboard` | group `Can_View_RTO_Dashboard` or ADMIN |
| `/rto-dashboard/create/` | `create_rto` | group `Can_View_RTO_Dashboard` or ADMIN |
| `/rto-dashboard/search/` | `rto_dashboard_search` | group `Can_View_RTO_Dashboard` or ADMIN |
| `/rto/edit/<int:pk>/` | `edit_rto` | group `Can_View_RTO_Dashboard` or ADMIN |
| `/signup/` | `signup` | public |
| `/special-rates/` | `special_rate_requests` | group `Can_View_Special_Rates` or ADMIN |
| `/special-rates/review/` | `special_rate_requests_review` | group `Can_Review_Special_Rates` or ADMIN |
| `/team-activity/` | `team_activity` | group `Can_View_Team_Activity` or ADMIN |
| `/team-activity/export/` | `export_team_activity_xlsx` | group `Can_View_Team_Activity` or ADMIN |
| `/sso/consume/` | `sso_consume` | public |
| `/tickets/` | `ticket_dashboard` | group `Can_View_Tickets` or ADMIN |
| `/upload-extract-pdf/` | `upload_extract_pdf` | group `Can_View_OCR_Pipeline` or ADMIN |
| `/upload/` | `upload` | group `Can_Upload_CSV` or ADMIN |
| `/user-management/` | `user_management` | staff, superuser or ADMIN |

`/admin/...` is Django's own admin site (staff accounts). With `DEBUG=True` Django also serves `/media/...` without a login; production serves media from object storage instead.
