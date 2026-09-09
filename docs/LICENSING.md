# Imagery licensing

The product needs four rights stacked together:

1. a photograph of a **specific address**
2. the right to create a **derivative work** (the AI edit)
3. the right to use it in **print**
4. for **commercial advertising directed at the property owner**

Almost no commercial imagery license grants all four. This document records
what was checked and why the project sources from state GIS.

## Rejected — commercial providers

| Provider | Verdict | Term |
|---|---|---|
| Google Street View | **Prohibited** | *"Street View imagery may not be used for any print purposes. This includes: … Advertisements or promotional materials of any kind."* Also bars screenshotting and significant alteration. Google states it grants no exceptions. |
| Google Maps satellite / Earth | **Prohibited** | *"may not be used for any commercial or promotional purposes."* |
| Vexcel | **Prohibited** | EULA defines "Commercial Purpose" to include *"advertising, marketing materials"*; the "Derivatives" definition excludes *"any portion of the images or pixels themselves"* — pixel-level editing falls outside the derivative right entirely. |
| Nearmap | **Prohibited** | Internal purposes only; bars creating a commercial imagery dataset or derivative works from Nearmap data. |
| EagleView / Pictometry | **Prohibited** | *"do not modify the Copyrighted Materials in any way"*; internal use, no redistribution. |
| Mapbox | **Prohibited** | *"shall not use Licensed Map Content in print, static digital or video media."* Product terms also bar derived works and AI use. |
| Bing / Azure Maps | **Unusable** | Bing is the one major vendor publishing print-advertising rights, but caps at 5,000 copies per image and permits *"no alteration except to resize"* — which forecloses the AI edit. Azure Maps has no street-level imagery at all. |
| Apple Look Around | **Unusable** | Display-only viewer, no image export. |
| HERE | **Unusable** | No public street-level API; platform terms bar modifying HERE Content. |
| Mapillary | **Impractical** | CC BY-SA 4.0 does permit commercial use and modification — the only permissive street-level license found. But ShareAlike would attach to the edited creative, attribution must be printed on the piece, and residential-street coverage runs 2–5%. |

## Rejected — MLS listing photos

Three independent failures, any one dispositive:

- **Copyright sits with the photographer**, not the agent, MLS, or data vendor.
  A vendor cannot sublicense rights it never held — which is why every
  MLS-sourced API hedges with "subject to MLS rules" and disclaims
  non-infringement.
- **IDX licenses permit display only**, on websites and apps. Print is not
  enumerated; modification and redistribution are barred. Some MLSs
  categorically refuse entities marketing to homeowners.
- **Case law is adverse.** *VHT v. Zillow* (9th Cir. 2019) rejected fair use
  for listing photos used for the same depictive purpose; *Warhol* (2023)
  forecloses the "AI made it transformative" argument; *Stross v. Redfin* puts
  the burden of proving a license on the user.

Exposure is **$750–$30,000 per photograph**, up to $150,000 if wilful.

## Rejected — federal aerial

**NAIP** is genuinely public domain (USGS: *"USGS-authored or produced data …
are in the U.S. Public Domain"*), free, and needs no key. But at 0.6 m
resolution an individual house is not recognisable to its owner, which
destroys the entire premise. Fine for lot context, useless as a hero image.

## Accepted — state GIS

| State | Resolution | License | Per-address API |
|---|---|---|---|
| **Indiana** | 3 in | **CC0-1.0** | yes |
| **Connecticut** | 3 in | **CC0-1.0** | yes |
| **North Carolina** | 6 in | *"free to use by anyone without restriction"* | yes |

Indiana's imagery metadata carries the license field `CC0-1.0` directly.
Connecticut's catalogue returns `CC0-1.0` on every imagery record. North
Carolina's GICC disclaimer states data from the NC OneMap geoportal is
*"free to use by anyone without restriction"* and that written release
agreements *"are not required and will not be issued."*

CC0 is the strongest position available: commercial use, derivative works, no
attribution burden. Attribution is printed anyway as a courtesy.

### Traps found while evaluating

- **Texas** reports `license: CC0-1.0` on all 100 imagery collections in its
  catalogue API — but the flagship statewide 6-inch service is Hexagon-hosted,
  subscription-only ($6,000–$375,000/yr) and restricted to Texas government
  entities. The CC0 tag is a database default that does not reflect the actual
  license. Trusting that field would be a serious error.
- **Utah** HRO imagery is licensed *from* Hexagon and explicitly excludes
  private individuals and contractors.
- **Pennsylvania** PAMAP forbids for-profit use and requires that *"any
  modifications to this data must be described"* — aimed squarely at AI editing.
- **North Carolina G.S. § 132-10** permits counties to bar commercial resale of
  GIS data. State-level NC OneMap is unrestricted; **county** portals may not
  be. Source from the state service and retain provenance.

## Alternatives if nationwide coverage becomes necessary

- **Bee Maps / Hivemapper** — owns its street-level pixels outright (unlike
  resellers), publishes $0.005/image. Print and derivative rights are
  unpublished; a bespoke license is plausible because they hold the copyright.
- **CycloMedia** — genuine 100 MP street-level capture, owns its imagery,
  licensed city-by-city. Verify US residential coverage before engaging.
- **Commissioned capture** — cleanest rights, grounded in 17 U.S.C. § 120(a),
  which permits photographing a building ordinarily visible from a public
  place. Requires explicit work-for-hire assignment. Runs $30–100 per property,
  which is prohibitive against a $0.82 piece.

## Maintenance

License terms change. Before any campaign expansion:

1. Re-read the source's license field and terms page.
2. Record the check date and the exact license string in `config.py`.
3. Preserve provenance for every image fetched, so any piece can be traced to
   the source and license under which it was obtained.
