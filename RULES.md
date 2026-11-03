# What Gary does with the rules

How Gary plays Dungeon World: what happens at the table, and which numbers Gary rolls and
applies so the GM (Gemini) doesn't have to. Commands are in the [README](README.md); how
it is built is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

- [Starting an adventure](#starting-an-adventure)
- [Characters](#characters)
- [Playing a turn](#playing-a-turn)
- [Fights](#fights)
- [Rest and travel](#rest-and-travel)
- [Towns, gear and treasure](#towns-gear-and-treasure)
- [The GM's tools](#the-gms-tools)
- [What stays with the GM](#what-stays-with-the-gm)

---

## Starting an adventure

`/begin` runs Dungeon World's first session the way the book advises: prepare little, and
build the world at the table from the players' answers. It follows a well-known recipe
("My Recipe for Starting Adventures", Spouting Lore): a **premise**, a fantastic place and a
grabby activity, chosen from Gary's offers or written by you; **characters**, made with
`/new`; **introductions**, each character's name, class and look; **hook questions**, one or
two to each character, with hard edges ("why do you trust her now?"), about the premise and
how the characters came together, whose answers Gary records as people, places and dangers;
**bonds**; and a **first scene**: a tense situation that demands action, drawing on your
bonds, with a question to one of you right away and "What do you do?". Whoever ran `/begin`
presses **Next** at each step; `/begin` again shows where it has got to, and `/undo` steps
back.

**One-shots.** `/begin` first asks whether this is a campaign or a one-shot. A one-shot follows
John Aegard's "Dragonslaying on a Timetable": the premise's place is the **final destination**;
the characters already travel together, so there are no introductions; the questions build the
setting, each character's stake and the enemy waiting at the end. Then three scenes, moved on
with **Next scene**: an arrival, at first sight of a strange mob a place or two short of the
destination; a branch, with three routes and something to outwit; and the finale at the
destination against the enemy the answers named. Before the finale comes a **level-up break**,
where everyone gains a level whatever their XP. **Epilogue** goes round the table for each
character's ending, and `/end` closes the game with its chronicle — no fronts, which a single
session has no room for. If the table wants more, `/begin` carries the same characters on as a
campaign.

After the first session, `/end` finishes each session and `/begin` starts the next: Gary
recaps where things stand, names anyone who can level up, asks any Barbarian the Outsider's
question about their homeland (answering marks XP, once a session), and reframes the scene
with "What do you do?".

The book is firm that fronts wait until the first session is over ("don't create any yet"),
so until the first `/end` the GM only notes the threats that come up. Then it writes
a **campaign front** (the threat that runs session to session, blanks and all) and one or two
**adventure fronts** from those notes and from what play turned up.

## Characters

### Making one

Everyone runs `/new`. Gary opens a form only you can
see: pick a class and a race, place the Dungeon World stats (16, 15, 13, 12, 9, 8) one at
a time, choose your class's starting gear (a Fighter builds their signature weapon), and
name your character. Gary works out your hit points and damage die, saves the
sheet, and tells the table who has joined. `/sheet` shows yours again.

Making a character also claims it: its checks are yours to roll, and nobody else's. Why it
matters: without a claim the model only knows Discord display names, and a check goes to
whoever happened to be speaking. `/playing character:` claims a character without
a sheet, for a table that keeps its sheets elsewhere.

The sheet tracks what rolls need — class, race, stats, HP, damage, armor, level — and your
gear. Gear does real work: your best armor plus a shield is your armor; a weapon's +damage
and piercing go into your damage roll; Hack and Slash needs a hand, close or reach weapon
and Volley a near, far or thrown one (and ammo for a bow); taking several shots on a Volley
spends ammo. A character made before gear existed gets a **Choose gear** button on `/sheet`.


**Alignment.** `/new` asks for one from your class's list, each with what fulfils it
("Good: endanger yourself to heal another"); the **Alignment** button on `/sheet` changes it
whenever your view of the world shifts. The GM sees it.

**Class choices.** `/new` also asks what your class decides: a Cleric names their god
and picks a domain and precept, a Druid their land and tell, a Bard their lore, a Thief
their poison (three uses to start), a Fighter their weapon's look — and an Elf Fighter a
weapon type that is always precise, so it hacks and slashes with Dex. A character made
before gets a **Choices and look** button on `/sheet`. The GM sees them all; a Thief's poison is spent with each use
and brewed three at a time somewhere quiet.

**Look.** `/new` then asks how your character looks: one from each of your class's lists in
the book (eyes, hair, clothes, body), and a line of your own if you like ("a scar through one
eyebrow"). It's on `/sheet`, in the introductions `/begin` posts, and in front of the GM every
turn, so the way you're described stays the way you chose.

**Spells.** A new Cleric or Wizard gets a **Choose spells** button after joining: a Wizard
fills their spellbook (three first-level spells), a Human picks one spell from the other
class's list, and both choose what they hold — total levels up to level + 1. Rotes and
cantrips come free. A caster made before spells existed gets the same button on `/sheet`,
which also shows what you hold, what's lost and what's running.
New spells come only with an undisturbed hour in the story — say your character prays or
studies, and the GM calls Commune or Prepare Spells (refused mid-fight, like camp), which
clears your spells and posts the button again. Casting is a move: +Wis for a Cleric, +Int
for a Wizard, only a spell you hold. On a 7–9 you choose attention, −1 ongoing to cast
until you next prepare, or losing the spell until then. Each running ongoing spell costs −1
to cast (Invisibility, Dominate and Mimic forbid casting) until the GM ends it. Gary rolls
what spells do to numbers: Cure Wounds heal, Magic Missile and Fireball (one roll on everyone
caught — "your target and everyone nearby", friends included — ignoring armor) and Harm (which
burns you too) deal damage, Bless gives its target
+1 while creatures stand, Magic Weapon adds 1d4 to the caster's weapon. The rest the GM
narrates from the spell's text.

Spells that take hold of a creature mark it, and Gary keeps to it: Sleep (1d4 of those you
name), Charm Person, Hold Person, Cage, Cause Fear, Polymorph and Dominate. An asleep, held or
caged creature can't attack. Pain wakes a sleeper and breaks Charm and Hold; each blow costs
Dominate a hold (1d4 to start, one spent per command, the spell ends at none). Ending a spell
frees the creature. Spells also end themselves: Invisibility when its target attacks, Alarm
when you next prepare, anything "until you cast" when you cast again. **Mirror Image** takes a
blow on a d6 of 4–6 and is gone. **Summon Monster** and **Animate Dead** roll their traits; the
creature joins you as your character with the basic moves, and is destroyed at 0 HP or
dismissed when the spell ends. **Contingency** holds a spell and its trigger (no more words than
your level) and releases it without a roll. Spells laid on a place — Cloudkill, Darkness,
Sanctuary, Alarm — stay where they were cast: Cloudkill's extra 1d6 lands on anything hurt
there (creatures and characters, by any blow), and it ends once the party leaves, as it lasts
"so long as you can see the affected area".

**Animal Companion.** A Ranger presses **Choose companion** on `/sheet` (Gary reminds them after `/new`): a
private form for species, base, strengths, trainings and weaknesses — as many as its
numbers say — and a name. When you work with it ("Fang and I go for the goblin"), the GM
flags the move and Gary checks the training and applies Command: +ferocity to damage and
its armor against the blow back on an attack (fight humanoids or monsters), +cunning to
Hunt and Track (hunt, search), Discern Realities (search, scout, guard) and Parley
(perform). The roll line says so ("+1 Fang"). Its instinct on someone interfering with you
is left to the table.

**Shapeshifter.** A Druid who calls on the spirits rolls +Wis into the form named ("into a
bear"): hold 3, 2, or 1 even on a miss. The GM names the form's moves, which Gary records
and shows on your sheet ("in the shape of a bear (hold 2: maul, roar)"); each use costs 1
hold with no roll, and at 0 you turn back. You can turn back early. Which species you
know (your land, Studied Essence) is the GM's to judge; your land is one of your class choices.

**Bonds.** Once there's another character at the table, press **Bonds** on `/sheet`: a private form
offers your class's bond lines ("___ has insulted my deity; I do not trust them"), you pick
the companion who fills each blank, and save. Write at least one; remove one to rewrite it.
Aid or Interfere names who you help or hinder and rolls +Bond — the number of bonds you
have with them. `/sheet` shows your bonds, and the GM sees them.

**XP.** Every miss marks 1 XP — Gary says so under the roll, with your total ("5/8 XP").
When you wrap up, run `/end`: anyone can flip the three questions (learned
something important, overcame a notable enemy, looted a memorable treasure), and each
player ticks whether their own character acted on their alignment (only they can). A
player can also **Resolve a bond**: pick it privately, and the companion it names gets an
Agree button only their player can press. Award XP gives everyone 1 XP per yes, plus 1 for
alignment and 1 for a resolved bond — which is cleared, so write a new one from `/sheet`. `/sheet` shows XP against what you need
(level + 7).

**Level Up.** With enough XP, `/sheet` shows a **Level up** button — refused while creatures stand or a move is
pending, because levelling takes downtime. A private form asks for a stat to raise (18 at
most; Constitution raises HP too), an advanced move from your class's list (the 6–10 list
opens at level 6; "Requires" and "Replaces" are honoured), and for a Wizard a spell for the
spellbook. A move that names a spell asks for it next: Chosen One and Prodigy (and Anointed
and Master) make that spell count one level lower — a first-level one becomes a rote or
cantrip, always ready — and Expanded Spellbook puts a spell from any class's list in a
Wizard's book. Multiclass Dabbler, Initiate and Master ask for a class and then one of its
moves, your level counting as one lower; the move is yours from then on — shown to the GM,
counted where Gary applies it, and if Gary rolls it (Backstab, Called Shot) you can make it.
Spellcasting moves and the Ranger's companion aren't offered: Divine Favor (Paladin) and God
Amidst the Wastes (Ranger) are how a non-caster gets spells — they make you a cleric of level
1 for spells, one level higher with each level after. Name your deity and choose spells from
the buttons on `/sheet`, and commune and cast like a Cleric, with Wisdom. The XP is spent and the level is announced to the table.

**Beyond 10th level.** When a level 10 character has the XP for 11th, the Level up button offers
the book's three choices instead. **Retire to safety**: make a new character with `/new`; the
old one leaves the story. **Take on an apprentice**: make the apprentice with `/new` and play
them; the old character travels with them as their mentor, played by the GM, gaining no XP
(the book has you play both, but Gary seats one character per player). **Change entirely to a
new class**: keep your stats, race, HP and look, keep up to three of your advanced moves as
core to who you are, and take the new class's starting moves in place of the rest.

**Ending a campaign.** A campaign's arc is its campaign front. When that front is over, the
next `/begin` asks the table: **carry on** in the same world (the GM writes a new campaign front
after the next session), or **close the campaign**. Closing makes the next session an epilogue:
Gemini shows how the world has changed, then asks each character what becomes of them, and
each player tells their own ending. `/end` then posts the campaign's chronicle — the premise,
how many sessions, every character and what became of them, the threats and how they ended,
the people met — and the campaign is finished. `/reset` posts the chronicle too if you end a
campaign early, and keeps a copy of the campaign's files before clearing it.

**The Barbarian**, a ninth class from its own playbook, is made like the others: `/new` asks
which of its two starting moves you start with (Full Plate and Packing Steel, or Unencumbered,
Unharmed) and your two Herculean Appetites. When you roll while chasing one, the GM marks the
move and Gary rolls 1d6+1d8 instead of 2d6; if the d6 comes up higher, trouble follows your
heedless pursuit. A 10+ on What Are You Waiting For? adds +2 to your damage against every
creature standing; The Upper Hand gives +1 to Last Breath and makes the 7–9 your offer to
Death. Gary also applies Unencumbered, Unharmed's armor (bare and under your load), Full
Plate's ignored clumsy, On the Move, Healthy Distrust, A Good Day to Die and Indestructible
Hunger (a **Reduce damage** choice after a blow), and What Is Best In Life gets its own XP
button at `/end`. Still Hungry adds an appetite at level-up; Appetite for Destruction
and Kill 'em All take an advanced move from the Fighter, Bard or Thief; More! Always More!
swaps an appetite for XP. Appetite rolls are left out of `/dice`, as they aren't 2d6.

**Compendium classes** — Divine Bond from the core book, and the Player Compendium's Awakened
Mind, Bearer, Blob, Dragonslayer, Flux, Landed Gentry and Wraithlander — open when something
happens in the story: drink a dragon's heart-blood, return from the wraith lands, be granted a
hold. The GM records it, and at your next level the Level up form offers that class's starting
move instead of a class move (only that level: skip it and the chance passes). Once you have
it, the class's other moves count as class moves. Flux's Bind and Landed Gentry's Diplomat
roll like any move; Wraithlander's Arm pierces the living; Tireless Blood ignores Sick. A
Bearer writes bonds with their weapon (the Bonds form offers it), which Hunger adds to its
damage and Its Name Is Dread uses to parley, and the weapon's boons heal, teach or mark XP. A
Divine Bond writes bonds with a god and marks one off for its favor. A Dragonslayer has a Blood
stat: calling on it is +Blood forward to Str rolls and a 1d4 against giving in to dragon
nature; Dragon's Breath burns everything in its path, the breather too; Fury and Wake the Dragon
add to it. Faraday's Law charges a weapon (+1d4, -1 to Bind) and Velocitas Eradico hurls
anything with Con. Landed Gentry's stronghold goes on the map as a keep steading with your two
benefits and the GM's complication, and Call the Banners turns a recruiting miss there into a 7–9.
A Blob commands slime minions that make moves with their own Str, Dex and Con and deal d4; a
creature's blow splatters one at 0 HP, inert until scooped together. Ooze form is +1 armor and
-1 to Int, Wis and Cha, and each slime copy of yourself costs -1 to all three.

---

## Playing a turn

### Talk to Gary

Gary stays quiet unless addressed, so five players chatting do not cost five model calls.
He answers when:

- you **@-mention** him (either the user or his auto-created role — both work), **or**
- the channel is in `GARY_FREEFORM_CHANNELS`, **or**
- you have a **check open** — your next message is read as an answer to it.

**Pacing.** The book treats time "like in a movie where pacing depends on the circumstances",
and the GM is told to pace it that way: start scenes close to the trouble, end them once their
question is answered, and sum up the dull parts — a safe road, routine shopping, a quiet night
— in a sentence. Only travel through hostile ground is a Perilous Journey; a safe road is just
narrated. When the party settles into a village, town, keep or city with no fight on, Gary posts
what downtime there offers — buying and selling (what's for sale here), Supply, Recover,
Carouse, Recruit, Bolster, and levelling up for anyone ready — with what the place's tags change.

**Upward and downward beats.** Good moments and setbacks should alternate (Mike Shea's "Upward
and Downward Beats"): too many wins feel dull, too many losses hopeless. Gary reads the recent
run from what it records (strong hits, creatures brought down, treasure and healing up; misses,
wounds, debilities and Last Breath down) and, only when the run leans hard one way, tells the GM
to make the next thing it introduces go the other way: a friendly face or a safe place after a
hard run, a stronger foe or an ambush after easy wins. The dice still decide every roll.

**Sharing the spotlight.** Dungeon World has no turns; the GM spreads the spotlight. Gary
keeps count of who has had the recent moments (speaking to Gary, making a move) and shows the
GM a tally every turn ("Gorm 8, Thorin 2, Liriel 0"), naming whoever has been quiet so the
next question, threat or opportunity goes to them.

### A turn, end to end

```
@Gary  I want a look at what the wardens are guarding, without them seeing me.
```

Gary decides your action triggers a move and asks you to roll:

```
Gary   **Caspian Varr** — Defy Danger, roll +Dex +2.   [🎲 Roll]
       The reeds are chest-high and the mist is with you, but the nearer warden keeps
       turning back towards the road…
```

You click **Roll**. Gary rolls 2d6 plus the stat from your sheet where everyone can see
it. The move fixed what each result means before you rolled — 10+ a strong hit, 7–9 a
weak hit, 6 or less a miss — and Gary narrates that:

```
Gary   🎲 **Caspian Varr** rolls 4 + 3 +2 = 9 — weak hit.
Gary   You make the far bank, but a stone turns under your boot. The warden's head comes
       round. Do you freeze, or run for the willows?
```

On a miss, Gary makes a GM move: something goes wrong, as hard as the fiction allows.

**Some results are yours to choose from.** A weak-hit Volley, a strong-hit Hack and
Slash, Discern Realities and Aid or Interfere all offer options. Gary posts them as
buttons, or as one dropdown when you pick several (Discern Realities' three questions),
and waits for you before narrating:

```
Gary   🎲 **Liriel** rolls 3 + 2 +2 = 7 — weak hit.
       Choose one (whichever you choose you deal your damage).
       Pick below, **Liriel**.
       [Move to get the shot…] [Take what you can get: -1d6 damage.] [Take several shots…]
```

Only you can roll your own move or make its choice, and only once: anyone else who clicks
is told privately whose it is. Buttons keep working across a Gary restart, like the moves
behind them — even between the dice and the choice.

If nobody rolls, the move expires after `GARY_CHECK_TTL` (default 180s) and is narrated as
hesitation or the moment passing — never as a success. If you rolled but never chose or
never rolled damage, the dice stand: Gary takes the first option(s), rolls the damage for
you, and says so. `/checks` shows what Gary is
waiting on, with a button to cancel your own.

**Forward.** "+1 forward" waits on your sheet with what it's for ("when acting on what
Discern Realities showed", "against the ogre") until the GM says a move acts on it — then
it's added and spent ("roll +Str +1 (+1 forward)"). Discern Realities, Take Watch, I Am the
Law, Backstab's advantage, Defend's "open the attacker up" and Arcane Art's +1d4 damage grant
them; the GM gives or takes them as the fiction earns it. They expire at camp, rest or the
start of a journey.

**Ongoing bonuses.** The GM can give "+1 ongoing" or "-1 ongoing" the way the book does — a
curse, a blessing, a lasting fear — to every roll or to one move. It's on your sheet and on
every roll line it touches, and it lasts for the battle (ending by itself when the fight
does) or until the GM ends it.

**Debilities.** When the fiction weakens you — a numbing claw, a sickening poison, a blow to
the head — the GM gives a debility: Weak, Shaky, Sick, Stunned, Confused or Scarred, each
-1 to one stat's rolls until cured, with the cause shown to the table. Your sheet lists
them and every roll shows the cost ("+Str +1 (-1 Weak)"). The GM removes one when magic or
real treatment cures it.

**Race moves with numbers** are applied too: an Elf Ranger's journey job through wilderness
succeeds as a 10+ without a roll; a Human Ranger eats no ration camping in a dungeon or
city; a Dwarf Fighter parleys with Con over a shared drink; a Halfling Fighter using their
size to Defy Danger and a Human Thief on criminal matters take +1. The GM says when the
fiction fits.

---

## Fights

**Fights have numbers.** When creatures appear, Gary puts them in the scene from the Dungeon
World bestiary — "Goblin 1, Goblin 2", each with its own HP — and the GM is shown what each
kind wants and does: its instinct, its moves, its qualities (goblins: *to multiply*; *call
more goblins*). A creature the GM makes up is built by the book's Making Monsters questions —
how it hunts (horde, group or alone), how big it is, its defence, what it's known for, what
its attack is like, what else describes it — and Gary works out its HP, armor, damage and tags
from the answers, so it's as tough as the book says such a thing should be; it gets an
instinct and moves too. Fights aren't balanced, by design: the book says your first duty is
to the fiction and to a real threat, "not to make a balanced fight". Most creatures are arrows
the GM fires at you, but one that earns the spotlight — it fled swearing revenge, Spout Lore
found its story, it beat you — gets a name ("Goblin 3" becomes Grizzlegut), keeps its wounds,
and is noted as a threat the fronts may take up between sessions. Creatures can hurt each other
too — the ogre swatting a goblin, rival bands clashing — with the attacker's damage through the
target's armor. A creature can't hurt what is out of its range. Hack and Slash and Volley are
aimed at one of them. A hit gives you a **Roll damage** button; your class's damage die
(plus or minus a d6 if your choice said so) goes through its armor, and a weak-hit Hack and
Slash means it hits you back in the same breath:

```
Gary   **Thorin**, roll your damage (d6) against Goblin 1.   [⚔️ Roll damage]
Gary   🎲 **Thorin** damage — d6: 5 → Goblin 1: 4 through armor 1, HP 3 → 0 — **defeated**.
```

**Packs and free blows.** When several creatures strike one character at once, the GM
rolls them as one blow, as the book says — the highest damage among them, rolled once, +1
for each attacker beyond the first — so every extra attacker hurts a little more. And when the fiction hands
you the blow (a pinned, helpless or surprised enemy), the GM has you just deal your damage,
no roll, with the reason shown.

**Defend.** Defend names who or what you protect ("Defend Eva", "Defend the gate"): hold 3
on a 10+, 1 on a 7–9, shown on your sheet. When a creature attacks you or the character you
defend, Gary rolls the blow and holds it while you choose — redirect it to yourself, halve
it, open the attacker up (+1 forward to an ally), strike back for your level, or spend
nothing. No answer spends nothing. Hold runs out, or ends when you make camp.

**Answering a blow.** Some moves let you turn a hit once it lands: a Wizard's Spell Defense
(end an ongoing spell, take its level less), Armor Mastery (your armor or shield takes it and
loses a point), Bloody Aegis (a debility instead), Shed (drop your animal form), Man's Best
Friend (your companion takes it), a Cleric's Divine Intervention (Commune's hold, for you or
an ally) and Penitent (take more for +1 forward to cast). When someone at the table could
use one, the hit carries a **🛡️ Reduce damage** button. It shows you, privately, only what
you can do about that hit, and gives the HP back at once — never more than the blow took.
The fight never waits: ignore it and you take the blow. Only the latest hit on someone can
be answered, once, and answering a blow that put them down cancels their Last Breath.
A Human Fighter's button also offers to **reroll** a creature's damage on anyone — once per
battle, and the new roll stands, better or worse.

A creature is defeated at 0 HP and not before, however the narration reads. When a
creature hurts you as the GM's move, Gary rolls that too and takes it off your sheet's HP.
So does anything else that hurts you — a fall, a trap, fire, a collapsing ceiling: the GM
says how bad, as the book does (bruises d4, some blood d6, broken bones d8, could kill a
commoner d10), and Gary rolls it through your armor, plus any cover, unless it's big enough,
magical or poisonous enough to ignore armor. It can be answered with Reduce damage like any blow.
`/undo` takes a blow back like any other change.

**At 0 HP you roll Last Breath** — Gary posts the button the moment you fall, and rolls it
for you if you don't click. +nothing: Death doesn't care how tough you are. On a 10+ you
cheat death and wake at 1 HP. On a 7–9 the GM describes what you glimpse beyond the Black
Gates and names Death's price; then you choose — **Take** the bargain and stabilize at 1
HP, or **Refuse** and pass on. On a miss you are marked as Death's own, and the GM will
tell you when. Anyone down or dead can't make moves.

**Clumsy armor** (scale mail, plate) gives -1 to every roll while worn, shown on the roll
line — except for Fighters and Paladins, whose Armored move ignores it.

**Poisons** from the equipment list can be used by anyone who carries a dose — spent, and
Dangerous unless it's the one a Thief mastered. On a creature, Bloodweed takes 1d4 off its
every damage roll and Serpent's Tears makes everyone's damage against it roll twice, better
kept — and the same goes for you if you're poisoned: it stays on your sheet until it wears off
or antitoxin cures it (one poison a dose, the one you name). A Thief's poison moves work too:
**Poison Master** (a poison you've used before is no longer dangerous to you), **Envenom** (a
safe poison on your weapon works by touch), **Brewer** (three doses of any poison you've used)
and **Alchemist** (that, or a poison of your own design, with the GM's caveats). **Stun damage** costs no HP, but until the GM says you've cleared your head, anything
you do is Defy Danger. **Cover** counts as armor against a creature's blow (+1 partial, +2
major). Dwarven Hardtack only feeds Dwarves. Cloudkill adds a separate 1d6, ignoring armor,
to every creature's wound while it lasts; Consume Unlife drains an undead creature to heal you.

**A Paladin's Quest.** Swear it in play — what you set out to do and up to two boons — and
the GM records it and names your vows. It shows on your sheet. While it lasts, Smite and Holy
Protection (and their stronger versions) count, and freedom from hunger means no rations at
camp or on a journey; the other boons are the GM's to honour. The GM ends it when it's
fulfilled — or when you break a vow.

**Exterminatus.** Promise aloud to defeat an enemy and the GM records it: Gary adds 2d4 to
your damage against it and takes 4 off your damage against anyone else, until it falls —
which ends the promise by itself. If it escapes or you give up, the promise holds until
you redeem yourself and the GM releases you.

---

## Rest and travel

**Inns, meals and services.** The book prices more than equipment: a week at an inn (peasant,
civilized or the fanciest in town), meals and feasts, escorts, a chirurgeon, passage, horses,
carts and boats, land and buildings, bribes, gifts and finery. Gary charges those prices,
less your Charisma where the book says "−Charisma", less a Minstrel's skill in town. A night
at an inn is a seventh of the week's price (Gary's arithmetic; the book prices weeks). A horse,
a cottage or finery you buy to keep goes on your sheet.

**Mounts and pack animals.** The book gives a mount a load (a horse 10, a cart and donkey 20)
and no other rules. A mount you own adds its load to what you can carry while it's with you;
leave it stabled or tied at a dungeon's mouth and the GM marks it left behind, so the weight is
yours again (`/sheet` shows it). Riding, a spooked horse or a wounded one stay in the story.
The Ranger's animal companion is different: a full character of its own (ferocity, cunning,
armor, training), built from `/sheet` by the book.

**Make Camp heals.** When the party beds down, Gary eats a ration for each of you and, if
the night is quiet, heals everyone half their max HP and moves the clock eight hours. A night
at an inn or as someone's guest costs no ration — "only mark off a ration if you're eating
from the food you carry". Rations are a day's food and water together; the book has no
separate thirst rule. Run out and you go hungry: the book sets no penalty, so you still heal,
and the GM makes something of the hunger instead (weakness, a short temper, a debility if it
goes on). Gary notes when the party last slept; past a day without it, the GM is reminded,
and weariness is the GM's to make something of — the book has no rule for it, so there's no
fixed penalty. If something comes in the night, whoever is on watch rolls
**Take Watch** (+Wis) and nobody heals. You can't make camp with creatures still standing or while
anyone's roll is unresolved (a Last Breath above all); anywhere else unsafe — a chase, a
storm — the GM refuses the rest or lets something come for you. Every camp says where it is
and why it's safe, so a bad one is easy to spot and `/undo`.

**Recover.** Days of rest somewhere comfortable and safe — an inn, a temple — are Recover,
not camp: the GM says how many days and whether a healer tends you, and the clock moves
that far. A day heals everyone resting to full; every three days clears a debility (every
two under a healer), and when there are more debilities than the rest clears, you pick
which with buttons only you can press.

**Healing items** work as the book says. A healing potion heals whoever drinks it 10 —
yourself, or a friend you pour it into — or removes one debility instead, if you'd rather. Bandages heal *someone else* 4, poultices and herbs
heal anyone 7; both spend a use and are slow, so not while creatures are still standing.

**Encumbrance.** Your load is your class's base plus Str (a Fighter 12+Str, a Wizard
7+Str); everything you carry has a weight, and 100 coins weigh 1. The sheet shows "weight
11/13". One or two over and every move takes -1; more than two over and the roll waits on
your choice — drop something (a button per item) and roll at -1, or keep it all and fail.

**Perilous Journey.** Travelling through hostile territory, the GM gives the jobs to
different characters — trailblazer, scout, quartermaster — or asks who takes which. Rations
for the expected days are packed at once, and each job's holder rolls +Wis with their own
button: a trailblazer's 10+ shortens the trip, a scout's lets you get the drop on trouble,
a quartermaster's saves a ration. A job nobody holds counts as a 6, so a lone traveller
takes one and the GM makes moves for the rest. The GM moves the clock for the trip's real
length.

---

## Towns, gear and treasure

**Loot, buying and selling.** Pick something up in the fiction and Gary adds it to your
sheet. If the book lists it — a long sword, plate, a bundle of arrows — it comes with its
real stats, so a found long sword hits harder and plate is armor at once; anything else (a
silver locket) is carried with no effect on the rules. Coin is on your sheet. Buying costs
the book's price, refused if you can't afford it; selling fetches half, or less from a
stingy buyer, never more. Dropping a weapon or armor changes what you can do immediately.
Every pickup, purchase and sale is posted to the table and undoes with `/undo`.

**Town.** Buying something readily available is the book's price. Something special, rare
or magical is the **Supply** move first (+Cha): a 10+ finds it at a fair price, a 7–9 costs
more (at most twice the book's) or something similar is offered, and for an item the book
doesn't price the GM names it, up to 500. **Carouse**: return triumphant and throw a party
for 100 coins or more, +1 for each extra 100; pick what comes of it (a useful friend,
rumours, information, not being tricked) — three on a 10+, one otherwise, and a miss gets
out of hand. The coin goes when the party does.

**Steadings.** Villages, towns, keeps and cities are made by the book as the party reaches
or hears of them: each kind's defaults (a village is Poor, Steady, Militia, with a Resource
and an Oath), one option and one problem. One made while you're in no steading is where you
are; after that the GM records which one you're in, and its tags count: a Dirt village sells nothing, a Poor one only the bare necessities, a Moderate town
most mundane goods but not plate; a Market widens what's for sale and gives +1 to Supply;
a Resource is half price and a Need fetches double; Arcane, Divine or a matching Guild gives
+1 to Recruit an adept, a priest or that guild's kind. Steadings change as the story and the
book's between-session rules change them, and `/state` lists them all.

**Treasure** is rolled by the book. When you search a creature's belongings or lair, Gary
rolls its damage die (twice for a hoarder, +1d4 for a lord or something ancient) on the
book's treasure table and rolls every amount: a few coins, a gem worth 2d10×10, a chest
worth 3d6×100, a dragon's hoard. The coins and valuables go straight onto the finder's
sheet; a gem or a crown sells for exactly what it is worth. What else turns up — a useful
item, a clue, a portal, something magical — the GM names, and for anything magical the GM
is shown a few of the book's 39 magic items for ideas. A creature is searched once.

**Day, night and weather.** The book has no rules for them, so they stay in the story — but Gary
keeps them straight. The GM is shown the light with the clock ("18:30, dusk"), worked out from
the hour and the season, so a winter evening is dark early. Each day's weather is set once and
kept until the next day, so it doesn't change at random; the story can still change it. A Druid
with Weather Weaver is asked at sunrise what the day's weather will be, and it comes to pass.

**Adventuring gear, torches and the dark.** The book has no rules for light, vision or darkvision:
darkness is part of the story, met with Defy Danger, Discern Realities and the GM's moves ("the
torch that lights the way also draws attention from eyes in the dark"). Torches, rope, chalk and
spikes come from adventuring gear: pull one out and Gary marks off a use, five to a pack, and
when the pack is empty you don't have one. Light and Darkness are spells, cast as usual.

**The rest of the dungeon gear** does what the book says. Open a **keg of dwarven stout** for
everyone and your Carouse takes +1 (the keg is gone). When your **bag of books** has just the
right book, Spout Lore takes +1 and a use is marked off. Share **halfling pipeleaf** with someone
and two uses buy +1 forward to Parley with them. Bandages, poultices, healing potions, antitoxin
and every ration count their uses as they're spent.

**The campaign map.** The book has the GM start a campaign map once the party first heads back
to civilisation, and keep drawing it. Gary keeps it: steadings and dungeons go on as soon as
they exist, the GM adds landmarks and terrain, and routes between places are measured in days
(a day is a ration) across the ground they cross — "two days across the moor" — so a journey
along a known route always takes as long. A road mentioned but never travelled stays a blank.
Fronts never appear on it by name; their signs do, as you'd see them — smoke over a village,
refugees on the road — and change as the threats do.

Between sessions the steadings change too, by the book's conditions. Gary applies the ones the
tags decide: **Collapse** (an emptying, poor steading shrinks a size), **Profit** (more trade
partners than prosperity: +prosperity) and **Surplus** (a resource one steading has and another
needs becomes trade between them, unless there's enmity). It works out **Opportunity** too —
whether a steading with an enmity is strong and close enough to attack. The rest turn on the
story — **Growth**, **Want**, blocked **Trade**, **Capture**, **Aid**, **Embattled**, **Clash** —
and the GM gets them as a checklist for its steadings to decide. `/map` shows it when you're not in a
dungeon (or `/map dungeon:World`).

Places on the map can carry one of the book's **monster settings** — "a type of location and the
monsters you might find there": Woods, Swamp, Hordes, Folk, Caverns, Depths, Undead, Experiments,
Planes. In the Greyfen (Swamp), the GM reaches for the Swamp's creatures first, as it does in a
dungeon with a setting. When the GM writes and reviews fronts it gets every setting's creatures and
what lives where, to populate dangers and the threats that are only tangentially related to them.

**Which weapon.** Range tags say how far a weapon is useful — hand (within your reach), close
(arm's reach and a foot or two), reach (several feet), near (the whites of their eyes), far
(shouting distance) — with no bonuses or penalties: the GM judges whether the distance fits. Say
what you attack with ("I stab him with my dagger") and that weapon's damage, piercing and magic
are the ones used; say nothing and Gary uses your best for the move. A thrown weapon is gone
once you throw it, whatever you roll, until you recover it: a spear leaves your sheet (its blow
still lands), throwing daggers count down one a throw. Gary remembers what's lying where it fell,
and when you pick it up you get back the very weapon — its magic and all — or the daggers you
threw. Move on without it and it's left behind for good. And Volley's "take several shots" isn't an
option with one — you can't mark off ammo with a weapon that's already in the air.

**Armor.** You wear your best armor; only the highest "n Armor" counts ("If you have more than one
item with n Armor, only the highest value counts"), and spare armor rides in your pack as weight.
"+n Armor" stacks: a shield in hand (one at a time) and anything else +n, like a warded cloak.
Clumsy is -1 for each clumsy thing in use — the armor you wear, not a suit you're carrying — and
Fighters and Paladins ignore it on armor. Take your armor off to swim, sleep or sneak (the GM
records it; `/sheet` says "armor off"), and moves for wearing no armor, like the Cleric's Divine
Protection, apply.

**Shields and two-handed weapons.** A shield is +1 armor on top of what you wear; you hold one,
so a second doesn't add. If you carry both a shield and a two-handed weapon (a halberd, a staff),
you have one or the other in hand: shield up (its armor, and you fight with a one-handed weapon)
or both hands (the two-handed weapon, and no shield armor). Switching is something you do in the
story — slinging the shield, taking up the halberd — and the GM records it; `/sheet` shows which.

**Dungeons.** Dungeon World doesn't draw dungeons in advance; the GM builds them as you
explore, with the book's Dungeon Moves (change the environment, point to a looming threat,
introduce a new faction or creature, use a threat from one, make you backtrack, present riches
at a price, present a challenge to one of you), made above all when you enter a new area or
miss a roll. Gary keeps the map, as the GM's principle "draw maps, leave blanks" asks: every
area you enter goes on it with what was found there, the way you came in and the exits you saw,
and an exit not yet taken stays a blank, waiting. The GM is shown the map every turn, so the
layout holds and backtracking works; you see it with `/map`. A dungeon can have a monster
setting from the bestiary (Caverns, Depths, Undead…), so what lives there belongs together.

**Making things.** Dungeon World has no crafting system, only moves. A Wizard's **Ritual** draws
on a place of power to create a magical effect: the GM sets one to four conditions (weeks of
work, help from someone, a lot of money, a lesser version, danger, something to disenchant).
Gary keeps the ritual on the Wizard's sheet with each condition ticked off as play fulfils it,
so its terms last between sessions; what it makes is recorded as a magic item. A Fighter with
**Blacksmith** can, at a forge, graft a magic weapon's powers onto their signature weapon, and
the magic weapon is destroyed. A Thief brews their poison with time and a quiet place. Or pay
for it: a custom item from a blacksmith is the item's price plus 50 coins, and repairs are a
quarter of its price.

**Magic items.** The book's are "ideas" and "ultimately for you to decide", and none is for
sale ("not for coin, anyway"). When the GM gives you one — from the book or invented — it goes
on your sheet as gear with its text: its weight counts, a magic weapon can be swung or
thrown, magic armor and shields protect, and Argo-Thaan gives a Paladin a d12 damage die. The
GM is shown what each one does every turn, so it works the same in session ten as in the
session you found it. `/picture` can draw any magic item the party carries.

**Hirelings.** Put out word that you're hiring and the GM calls Recruit: +1 each for
generous pay, saying what you're setting out to do, offering a share, and a useful
reputation. Whoever joins is made by the book — skills (Adept, Burglar, Minstrel, Priest,
Protector, Tracker, Warrior) with ranks, loyalty and a cost, their points set by where you
found them (village 5 up to city 10) — and kept on your sheet. As the book has it, no hireling's
best skill outranks your level: a level 2 character can't hire a rank 4 warrior. Ordering one into something
dangerous or degrading is Order Hirelings, +their loyalty (-1 while their pay lapses). The
GM sees each skill's rule, and Gary applies the ones with numbers: a hired Priest adds their
skill to camp healing and can give first aid (2×skill HP, -1 forward); a Warrior aiding your
attack adds their skill to damage and takes the counter-attack; a Protector helping you Defy
Danger gives +1 (but a 10+ counts as 7–9), or standing sentry adds their skill to your armor
and then tires until they rest; a Tracker guides any journey shorter than their skill to
success; a Minstrel takes their skill off prices in town. Burglars and Adepts are the GM's.

**Outstanding Warrants and Bolster.** Returning somewhere you've caused trouble, the GM
calls Outstanding Warrants (+Cha): on a hit everyone recognises you, and on a 7–9 the GM adds
a complication — a warrant, a price on your head, or someone you care about in a bad spot.
Spending weeks in study, meditation or hard practice is Bolster: a week or two gives 1
preparation, a month or more 3, shown on your sheet; each is +1 to one roll when the GM says
it pays off ("+1 preparation"). The clock moves the weeks.

---

## The GM's tools

**Fronts.** The GM writes the threats that move on their own as fronts, by the book's recipe:
a campaign front, the slow threat behind everything, and one or two adventure fronts about
here and now. Each has up to three dangers of the book's types (a Cult, a Dragon, a Dark
Portal…), grim portents, an impending doom (tyranny, pestilence, destruction, usurpation,
impoverishment or rampant chaos, made concrete) and a few stakes questions. As the book says,
fronts are written between sessions, not in play: after each `/end` the GM quietly
reviews them — advancing what the party neglected, ending what they dealt with, moving an
unresolved danger up to the campaign front, writing a new adventure front when one is
resolved. In play the GM notes new threats, ticks portents off (as play shows them, offscreen,
or as a hard move after a miss), and uses each danger's GM moves. Players never see fronts;
only people who can manage the server can open `/fronts` (or `/state`, which includes them),
and only they see the answer. Once a front is over (every danger stopped, or its doom come),
Gary shows the table what it held: what each danger wanted, which portents came to pass, which
never did, and what its doom was or would have been. Every change undoes with `/undo`.

**Pictures.** Anyone can ask for a `/picture` of a character, someone the party has met or heard
of, a creature in the scene, or a place they know — never anything still hidden. Gary draws it
from what's recorded (a character's look, a person's description, a creature's tags), always in
the same painted style, and keeps the first picture of each, so a face stays the same. `redraw`
draws a new picture of the same subject — the kept one goes along with it, so it's the same
person in a new pose and place, not a stranger. When a portrait of a person is made, Gary notes
what it shows ("a dark headscarf, a scar by her mouth, a coin pendant"), so the GM describes them
as drawn and later pictures start from it. Pictures of places and creatures leave people out,
so nobody's look comes from anywhere but their own portrait. New pictures are capped per day.

**People you know.** Every named person is recorded, and `/npcs` lists the ones the party has
met or heard of: each with how they feel about you (hostile, wary, neutral, friendly or
devoted) and a line on who they are. The GM's notes on them stay hidden, and so do people
that the GM keeps offscreen, like a hidden villain, until the party meets or hears of them.

**Instant NPCs.** When someone becomes important in the moment, the GM can have Gary roll the
book's Instant NPC tables: an instinct (what they want), a knack (how they might get it) and
a name nobody in the campaign has yet.

**End-of-session questions.** `/questions` swaps the book's three end-of-session questions
for up to three about the current adventure, as the book suggests; they're posted for
everyone, and `/questions default:True` restores the book's.

**Compendium requirements.** When the story meets a compendium class's requirement, the GM
records it and the character may take the class at their next level (see Level Up).

---

## What stays with the GM

Gary rolls, checks and records whatever has a number. What the book leaves to judgement
stays with the GM (Gemini), which is told the rule and what Gary already applied:

- **Story effects.** What a spell, a magic item, a boon or a move does in the fiction —
  Charm Person's friendship, Invisibility, a Cleric's petitions, a Bard's lore, Awakened
  Mind's projections. Gary ends running spells when the GM says (`end_spell`).
- **Moves with a judgement-call trigger.** Conditional bonuses like Fount of Knowledge,
  Heist, Reaper or Unforgettable Face ("+1 when acting on the answers"); the GM grants them
  as a forward or an ongoing bonus.
- **Holds Gary doesn't count.** Trap Expert, Evil Eye and similar holds are told to the GM as
  exact notes on the result; Defend's hold is counted, but only creature attacks spend it.
- **A few simplifications.** Cloudkill counts every creature in the scene (Gary can't see
  the fog's edge); Dwarven and Elven goods aren't marked in the equipment list, so their
  cheaper prices are the GM's; ammo is one count shared by bows and throwing daggers; poisons
  on characters aren't tracked, so what antitoxin cures is the GM's to say; magic items do
  nothing to the numbers.
- **Choices inside a few moves.** Bound for Life's choice at Last Breath, the Ranger
  companion's instinct when someone interferes with you, a Bearer's weapon resisting you
  (the GM gives the penalty), Taxation's income.
