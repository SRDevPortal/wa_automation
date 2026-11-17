import frappe


def is_manager(user=None):
    return (user or frappe.session.user) == "Administrator" or bool(set(frappe.get_roles(user)) & {"System Manager", "WA Automation Manager"})


def has_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    return is_manager(user) or doc.owner == user


def _query(doctype, user):
    if is_manager(user):
        return ""
    return f"`tab{doctype}`.owner = {frappe.db.escape(user or frappe.session.user)}"


def campaign_query(user=None):
    return _query("WA Campaign", user)


def owned_query(user=None):
    return _query("WA Campaign Run", user)


def recipient_query(user=None):
    return _query("WA Campaign Recipient", user)


def require_sender():
    if not is_manager() and "WA Automation Sender" not in frappe.get_roles():
        frappe.throw("WA Automation Sender role is required to send or activate campaigns.", frappe.PermissionError)


def require_app():
    if not is_manager() and "WA Automation User" not in frappe.get_roles():
        frappe.throw("WA Automation User role is required.", frappe.PermissionError)
