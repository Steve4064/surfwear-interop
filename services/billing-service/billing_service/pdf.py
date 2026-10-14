"""Mise en page PDF d'une facture (ReportLab)."""
from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OCEAN = colors.HexColor("#0E2A3B")
TIDE = colors.HexColor("#1C6E8C")
LINE = colors.HexColor("#D6E1E5")
MUTED = colors.HexColor("#587080")

SELLER = {
    "name": "Surfwear SAS",
    "address": "Zone artisanale des Pins, 40200 Mimizan",
    "siret": "SIRET 90012345600017 · TVA FR12900123456",
    "iban": "Règlement par virement : FR76 3000 4000 0500 0012 3456 789",
}


def euro(v: float) -> str:
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def frdate(d: date | str) -> str:
    d = date.fromisoformat(str(d)[:10])
    return d.strftime("%d/%m/%Y")


def render(inv: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"Facture {inv['id']}", author=SELLER["name"])
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, textColor=MUTED, leading=11)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=OCEAN)
    brand = ParagraphStyle("brand", parent=base, fontName="Helvetica-Bold", fontSize=14, textColor=TIDE)

    shop = inv["shop"]
    head = Table([[
        [Paragraph("Surfwear <font color='#0E2A3B'>Pro</font>", brand), Spacer(1, 3),
         Paragraph(SELLER["name"], bold), Paragraph(SELLER["address"], base), Paragraph(SELLER["siret"], small)],
        [Paragraph(f"Facture {inv['id']}", title), Spacer(1, 4),
         Paragraph(f"Date : {frdate(inv['issued_at'])}", base),
         Paragraph(f"Échéance : {frdate(inv['due_date'])}", base),
         Paragraph(f"Commande {inv['order_id']} · BL {inv['shipment_id']}", small)],
    ]], colWidths=[90 * mm, 84 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    client = Table([[[
        Paragraph("Facturé à", small), Paragraph(shop["name"], bold),
        Paragraph(shop.get("address") or shop.get("city", ""), base),
        Paragraph(f"SIRET {shop['siret']}" if shop.get("siret") else "", small),
    ]]], colWidths=[174 * mm])
    client.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))

    rows = [["Référence", "Désignation", "Qté", "PU HT", "TVA", "Total HT"]]
    for l in inv["lines"]:
        rows.append([l["sku"], Paragraph(l["name"], base), str(l["qty"]), euro(l["unit_price"]),
                     f"{l['vat_rate'] * 100:.0f} %", euro(l["total_ht"])])
    lines = Table(rows, colWidths=[26 * mm, 70 * mm, 14 * mm, 24 * mm, 14 * mm, 26 * mm], repeatRows=1)
    lines.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("BACKGROUND", (0, 0), (-1, 0), OCEAN),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    totals = Table([
        ["Total HT", euro(inv["total_ht"])],
        ["TVA", euro(inv["total_vat"])],
        ["Total TTC", euro(inv["total_ttc"])],
    ], colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"), ("LINEABOVE", (0, 2), (-1, 2), 0.8, OCEAN),
        ("TEXTCOLOR", (0, 2), (-1, 2), OCEAN), ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))

    status = "Acquittée" if inv["status"] == "paid" else f"À régler avant le {frdate(inv['due_date'])}"
    story = [head, Spacer(1, 10 * mm), client, Spacer(1, 8 * mm), lines, Spacer(1, 6 * mm), totals,
             Spacer(1, 10 * mm), Paragraph(status, bold), Spacer(1, 2 * mm), Paragraph(SELLER["iban"], small),
             Paragraph("Pénalités de retard : trois fois le taux d'intérêt légal. Indemnité forfaitaire "
                       "pour frais de recouvrement : 40 €. Pas d'escompte pour paiement anticipé.", small)]
    doc.build(story)
    return buf.getvalue()
