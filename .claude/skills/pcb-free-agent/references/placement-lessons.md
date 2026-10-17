# Placement lessons: what a quick route shows, and what to move

Read this when you review a placement: after a seed, between candidates, or in a loop
that routes, looks and moves parts. Each entry gives a placement rule, what its
violation looks like on a routed board, what to move, and the board it was measured
on (the boards are in the stress corpus).

When a review finds a new pattern that a move fixed, add an entry in the same shape.

**How to find the problems on a routed board:**
- **Open nets:** for each one, `python3 -X utf8 py_placer/converge.py where BOARD --nets
  NET ...` gives the gap and the copper walling it in.
- **Nets with many vias, and long detours:** compare each net's routed length with the
  span of its pads. A net routed at 1.5-2x its span with several vias is usually a part
  placed away from the pins it serves.
- **Two-pin parts whose airwires cross:** turning the part 180 degrees shortens both.
- `python3 -X utf8 py_tools/render_placement.py BOARD -o OUT.png --ratsnest-nets NET ...`
  draws just the failing nets' airwires.

**Move one cluster at a time, and route again.** Moves in several places at once
cannot be attributed: on bitaxe_ultra, seven moves together closed two nets and opened
six others.

## Power

- **A switching regulator's power stage is one tight cluster.** The high-side and
  low-side FETs face each other, with their switch-node pads toward each other, and
  the inductor sits right against the switch node.
  - *Looks like:* the switch node (a wide net) open or detouring, and the FETs and
    inductor at their default angles with the switch-node pads facing away from each
    other.
  - *Move:* turn the FETs so their switch-node pads face each other, and put the
    inductor beside them.
  - *Measured:* bitaxe_ultra. The placement had Q1 to Q2 at 12.0 mm, Q2 to L1 at
    12.7 mm and Q1 to L1 at 24.5 mm, all at 0 degrees, and Q2's switch-node tab faced
    away from L1. The human layout has 6.0, 8.5 and 14.4 mm, with the FETs at 90 and
    -90 degrees. With the cluster compacted to the human spacing, a quick route closed
    /Power/SW and the enable line beside it.

## Parts that serve a chip's pins

- **A part goes beside the pins it serves, on the side of the chip where those pins
  are.** Standing next to the chip's outline is not enough: a module has pins on
  several edges.
  - *Looks like:* open or long nets between a header or test point and one edge of a
    module, with the part on the module's opposite side.
  - *Move:* find the pins in the board (each pad's position and net) and put the part
    beside them.
  - *Measured:* bitaxe_ultra. The ESP32 module U12 has its IO0, P_TX and P_RX pins on
    its north edge. The programming header J5 and its test points had been placed
    against its south side, 20-35 mm from those pins. Moved beside the pins, all
    three nets routed.
- **A connector's test points and helper parts move with it.** A test point left
  behind is a long net of its own.
  - *Measured:* bitaxe_ultra. Moving J5 north and leaving P_RX's test point TP22
    south opened P_RX; moving TP22 to the pin closed it.

## Decoupling around fine-pitch chips

- **A fine-pitch chip's decoupling and stacked-domain caps go close to the pins they
  serve, but OFF the pin rows and the escape field.** About 2-3 mm out, on the far
  side of where the escapes run. This matters most when the caps are on the other face
  from the chip, since every connection then needs a via beside the pin.
  - *Looks like, too far:* each domain net (chip pin, two caps, a test point) spans
    15-25 mm and needs 3-6 vias.
  - *Looks like, too close:* the chip's own signals start failing. Caps on the pin rows
    take the escape tracks' room and the via sites.
  - *Measured:* bitaxe_ultra, the ASIC U14 on the back with its stacked-domain caps
    on the front. With the caps 8-14 mm west of the pins, the domain nets needed 3-6
    vias each. Caps directly over the pin rows opened two more nets: a domain net and
    the ASIC's RO signal. Caps 2-3 mm off the pin rows left no more nets open than
    before, with fewer vias (156 against 162 on a quick route).

## Orientation

- **Turn a part so the pads carrying its connections face the parts they connect
  to.** A seed keeps a part's input angle when it fits, and a pile's angles are
  arbitrary.
  - *Looks like:* a net that routes around its own part's body to reach the far pad,
    or two airwires crossing at a two-pin part.
  - *Measured:* bitaxe_ultra, Q2 above (its switch-node tab faced away from the
    inductor).
