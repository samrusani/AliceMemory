# Quenby Fieldworks

A fictional company used by the showcase bench. No real firm and no real person
appears in the notes. The generator is `company.py`. Regenerate a folder with:

```
python scripts/generate_showcase_company.py --size 1000 --out DIR
python scripts/generate_showcase_company.py --size 10000 --out DIR
```

The default seed is 20261013. The same seed writes the same files. The sizes
are exact. The dates run from 1 October 2024 through 31 March 2026.

Every note carries a team, a date, a sensitivity and a domain in its front
matter. Alice stores thirteen domains and has no domain named hr, so the HR
folder is stored as domain legal and sensitivity confidential. `legal` is a
restricted domain. The profiles `read_only_agent`, `project_scoped_agent` and
`memory_proposal_agent` may not read it. `trusted_local_agent` and
`admin_agent` may. A support key and a product key are therefore held back
from an HR note by the domain as well as by the confidential sensitivity. A
sales key may read the legal domain and is held back by the confidential
sensitivity alone. A pricing draft is for sales and leadership: domain
financial and sensitivity private. `financial` is a restricted domain too. A
published price stays internal and professional, because a support key may
read an internal note and must still be refused a draft price.

The generator writes `.showcase_company_owner` in a folder it owns. It refuses
a folder that already holds files and lacks that marker. It does not delete a
file it did not write. A file it wrote on an earlier run and that this run
does not produce is removed.

The planted notes hold one fact for each bench task: a fact stated once, a
decision that is later reversed, a customer whose status changes, two notes
that disagree, a fact that is complete only when a support note and a product
note are read together, an observation with summaries beside separate
observations, and an HR pay record plus a draft price that a support key must
not read. The strings the abstention questions ask about are not in any note.
