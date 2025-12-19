# Role-based access control

This release introduces explicit role memberships per building. Each membership links a user to a building (or globally) and grants a role with a predefined capability set. Capabilities can be extended or revoked per membership via JSON overrides.

## Roles

| Role | Scope | Default capabilities |
| --- | --- | --- |
| Technician | Assigned buildings only | Interact with their own work orders. |
| Backoffice Employee | Buildings they manage | Create/edit buildings, units, and work orders; mass assign techs; manage memberships for those buildings. |
| Lawyer / Адвокат | Global / Глобално | Read-only access to every building, can create or edit apartments, and files confidential legal work orders hidden from technicians. / Достъп само за четене до всички сгради, възможност за създаване и редакция на апартаменти и подаване на конфиденциални юридически поръчки, скрити от техниците. |
| Administrator | Global | All capabilities, including user management and audit visibility. |

## Capabilities

Capabilities are referenced in code via `core.authz.Capability` constants.

| Capability | Description |
| --- | --- |
| `view_all_buildings` | Visibility into every building/companywide data. |
| `manage_buildings` | Create/update buildings plus related metadata. |
| `create_units` | Add or edit units. |
| `create_work_orders` | Add or edit work orders. |
| `mass_assign` | Use the mass-assign workflow for technicians. |
| `approve_work_orders` | Approve, reject, or reopen work orders in the awaiting approval state. |
| `view_audit_log` | Access the human-readable audit trail UI. |
| `manage_memberships` | Invite/remove building members and adjust role overrides. |
| `view_users` | Access the internal user-management dashboard. |
| `view_confidential_work_orders` | View lawyer-only work orders / Преглед на адвокатските конфиденциални поръчки. |
| `manage_units` | (Same as create_units; included for completeness.) |

Use `BuildingMembership.capabilities_override` to fine-tune privileges. Its structure accepts lists of capability slugs under `{"add": [], "remove": []}`.

## Work order workflow

Technicians progress requests from **Open → In progress → Awaiting approval**, optionally adding a replacement request note so approvers know which materials or budget are needed. Backoffice employees or administrators (roles with the `approve_work_orders` capability) review orders in the awaiting queue and either approve them (transition to **Done**) or send them back to **In progress** with any follow-up work. The actor who submitted the approval request cannot self-approve unless they also hold an approver role.

## Auditing

Role/membership changes automatically emit `RoleAuditLog` entries. Administrators can review them via **Audit Log** in the main navigation or through the Django admin (`RoleAuditLog`).

## Auto-provisioning

- Building owners automatically receive a Technician membership for that building, pre-filled with their configured sub-role.
- Staff/superusers receive a global Administrator membership via migration.

You can manage memberships via Django admin (`Building > Memberships`) or by editing `BuildingMembership` records directly.
