# Glossary

MD — Measured Depth
:   Length along the wellbore from the depth reference (usually RT) to a point. Always ≥ TVD.

TVD — True Vertical Depth
:   Vertical distance from the depth reference to a point in the well.

TVDSS — True Vertical Depth Sub-Sea
:   TVD referenced to mean sea level: `TVDSS = TVD − RT elevation above MSL`.

KB — Kelly Bushing
:   Depth reference on older rigs: the top of the kelly bushing on the rig floor.

DF — Drill Floor; GL — Ground Level
:   Other depth references. Every wellbore stores which one its depths are measured from and its
    elevation above MSL.

Offset (analogue) well
:   An existing well used as evidence for a planned one. Chosen by distance where the
    trajectories cross the target formation, fault block, depth, design, mud, vintage and data
    quality.

n_eff — effective number of wells
:   Kish effective sample size of weighted offsets, (Σw)² / Σw². Ten wells of very unequal
    similarity may be worth three.

Beta-Binomial posterior
:   The hazard probability's distribution after counting weighted offset wells that did (and
    documented that they did not) have the hazard; its mean and 90 % interval are shown.

Relative position (rel)
:   Where a depth sits inside a formation: 0 at its top, 1 at its base. Carries an event from
    one well to another by formation rather than by depth.

Datum confidence
:   Whether a position's geodetic datum was stated (known), deduced from context (inferred) or
    not known; stored with an uncertainty radius in metres.

RT — Rotary Table
:   Depth reference on the rig floor. Used interchangeably with KB on many reports; always
    record which one and its elevation.

WCR — Well Completion Report
:   Final report for a well: geology, casing, tests, logs, completion.

DDR — Daily Drilling Report
:   Report of each 24 h of rig activity: depth, operations, mud, NPT.

Rig state
:   What the rig is doing now (drilling rotary or slide, reaming, circulating, tripping in or
    out, connection, static), from rules on hookload, bit vs hole depth, movement, flow, RPM and
    SPP. Detectors run only in the states where their signature means something.

MSE — Mechanical Specific Energy
:   Energy spent per volume of rock drilled (MPa). Rising MSE at steady parameters means the
    bit works harder for the same progress (balling, dull bit, vibration).

d-exponent (dxc)
:   Normalised drillability from ROP, RPM, WOB and bit size, corrected for mud weight; a falling
    trend while drilling shale suggests rising pore pressure.

Herschel-Bulkley
:   Mud rheology model τ = τ_y + K γⁿ (yield stress plus power law); used for annular pressure
    loss and ECD.

Soft-string model (torque & drag)
:   Johancsik model of string tension and torque along the trajectory with a friction factor;
    predicts hookload when picking up, slacking off and rotating.

Likelihood ratio (LR)
:   How much more likely the observed data are during a hazard than without it. Multiplies the
    prior odds into the posterior odds; independent of how common the hazard is.

ISA-18.2
:   Alarm-management standard: prioritised, rationalised alarms, no chattering or duplicates,
    controlled shelving, alarm-rate limits.

Shelving
:   Temporarily withholding a hazard's non-critical alerts, with a reason and an expiry; they
    are still recorded.

DATA STALE
:   No new sample for a few seconds: the live view turns grey so old numbers are never read as
    current.

WITS0, ETP, OPC-UA
:   Rig data transports: WITS level 0 (serial/TCP text records), Energistics Transfer Protocol
    (WebSocket streaming), OPC Unified Architecture (industrial automation).

Hybrid retrieval
:   Finding passages with two searches at once, full-text (words) and vector (meaning), merged by
    reciprocal rank fusion (each list contributes 1 / (60 + rank)), then reranked.

Text-to-SQL
:   A model writing the SQL query for a question. Here only over the read-only `chat.v_*` views,
    checked by a guard and run by a role that can read nothing else.

Number verifier
:   The rule that every number, depth and date in an assistant answer must equal a value in the
    evidence that sentence cites; otherwise the answer is regenerated or withheld.

NPT — Non-Productive Time
:   Rig time lost to unplanned events (stuck pipe, losses, equipment failure, waiting).

ECD margin
:   Planned ECD minus mud weight (friction while circulating); the linter adds it to the
    program's maximum mud weight before comparing with shoe tests and loss evidence.

ECD — Equivalent Circulating Density
:   Effective mud density at a depth while circulating: static mud weight plus annular
    friction pressure, expressed as a density.

LOT / FIT — Leak-Off Test / Formation Integrity Test
:   Pressure tests below a casing shoe. LOT finds where the formation starts to take fluid;
    FIT confirms it holds to a set pressure. Either sets the upper limit for ECD.

EMW — Equivalent Mud Weight
:   A pressure at depth expressed as the mud density that would produce it. LOT/FIT results are
    reported as EMW; stored in sg.

TOC — Top of Cement
:   Shallowest depth of cement behind a casing string.

Geodetic datum; Kalianpur 1975
:   The reference frame a latitude/longitude is given in. Older Indian well records often use
    Kalianpur 1975 (EPSG:4146, Everest 1830 ellipsoid) instead of WGS 84; the same numbers then
    point to a place several hundred metres away. `core.geodesy` converts with EPSG:1156
    (dX 295, dY 736, dZ 257 m), accurate to metres to tens of metres.

Sidetrack
:   A new wellbore drilled out of an existing one from a kickoff depth, e.g. after a stuck-pipe
    fish cannot be recovered. Stored as a `wellbore` with `parent_id` and `kickoff_md`.

Block, JV
:   Licence block (the area a licence covers) and joint venture (the partners sharing it). The
    two attributes that decide which rows a user can read.

RLS / ABAC — Row-Level Security / Attribute-Based Access Control
:   Postgres filters rows per user by policy; here the policy compares each row's block and JV
    with the attributes of the caller.

WITSML — Wellsite Information Transfer Standard Markup Language
:   Energistics standard for exchanging drilling data (logs, trajectories, reports).

eRTMAC
:   Real-time monitoring centre feed of rig data to onshore systems.
