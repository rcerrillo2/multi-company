# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    intercompany_picking_id = fields.Many2one(comodel_name="stock.picking", copy=False)

    def _action_done(self):
        for pick in self.filtered(
            lambda x: x.location_dest_id.usage == "customer"
        ).sudo():
            purchase = pick.sale_id.auto_purchase_order_id
            if not purchase:
                continue
            purchase.picking_ids.write({"intercompany_picking_id": pick.id})
            if not pick.intercompany_picking_id and purchase.picking_ids[0]:
                pick.write({"intercompany_picking_id": purchase.picking_ids[0]})
            for move in pick.move_lines:
                move_lines = move.move_line_ids
                po_move_lines = (
                    move.sale_line_id.auto_purchase_line_id.move_ids.filtered(
                        lambda x, ic_pick=pick.intercompany_picking_id: x.picking_id
                        == ic_pick
                    ).mapped("move_line_ids")
                )
                if not len(move_lines) == len(po_move_lines):
                    raise UserError(
                        _(
                            "Mismatch between move lines with the "
                            "corresponding  PO %s for assigning "
                            "quantities and lots from %s for product %s"
                        )
                        % (purchase.name, pick.name, move.product_id.name)
                    )
                # check and assign lots here
                for ml, po_ml in zip(move_lines, po_move_lines):
                    lot_id = ml.lot_id
                    if not lot_id:
                        continue
                    # search if the same lot exists in destination company
                    dest_lot_id = (
                        self.env["stock.production.lot"]
                        .sudo()
                        .search(
                            [
                                ("product_id", "=", lot_id.product_id.id),
                                ("name", "=", lot_id.name),
                                ("company_id", "=", po_ml.company_id.id),
                            ],
                            limit=1,
                        )
                    )
                    if not dest_lot_id:
                        # if it doesn't exist, create it by copying from original company
                        dest_lot_id = lot_id.copy({"company_id": po_ml.company_id.id})
                    po_ml.lot_id = dest_lot_id
        return super()._action_done()

    def button_validate(self):
        res = super().button_validate()
        company_obj_sudo = self.env["res.company"].sudo()
        is_intercompany = company_obj_sudo.search(
            [("partner_id", "=", self.partner_id.id)]
        ) or company_obj_sudo.search(
            [("partner_id", "=", self.partner_id.parent_id.id)]
        )
        if (
            is_intercompany
            and self.company_id.sync_picking
            and self.state == "done"
            and self.picking_type_code == "outgoing"
        ):
            sale_order = self.sale_id
            dest_company = sale_order.partner_id.ref_company_ids
            for rec in self:
                if rec.intercompany_picking_id:
                    rec._sync_receipt_with_delivery(
                        dest_company,
                        sale_order,
                    )
        return res

    def _sync_receipt_with_delivery(self, dest_company, sale_order):
        self.ensure_one()
        intercompany_user = dest_company.intercompany_sale_user_id
        purchase_order = sale_order.auto_purchase_order_id.sudo()
        if not (purchase_order and purchase_order.picking_ids):
            raise UserError(_("PO does not exist or has no receipts"))
        if self.intercompany_picking_id:
            dest_picking = self.intercompany_picking_id.with_user(intercompany_user.id)
            for move in self.move_ids_without_package.sudo():
                # To identify the correct move to write to,
                # use both the SO-PO link and the intercompany_picking_id link
                dest_move = move.sale_line_id.auto_purchase_line_id.move_ids.filtered(
                    lambda x, pick=dest_picking: x.picking_id == pick
                )
                for line, dest_line in zip(move.move_line_ids, dest_move.move_line_ids):
                    # Assuming the order of move lines is the same on both moves
                    # is risky but what would be a better option?
                    dest_line.sudo().write(
                        {
                            "qty_done": line.qty_done,
                        }
                    )
                dest_move.write(
                    {
                        "quantity_done": move.quantity_done,
                    }
                )
            dest_picking.sudo().with_context(
                cancel_backorder=bool(
                    self.env.context.get("picking_ids_not_to_backorder")
                )
            )._action_done()

    def _update_extra_data_in_picking(self, picking):
        if hasattr(self, "_cal_weight"):  # from delivery module
            self._cal_weight()
