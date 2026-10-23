select o.source, o.offer_id
from {{ source('core', 'offer') }} o
where o.first_seen_date > (
    select min(ob.snapshot_date) from {{ source('core', 'offer_observation') }} ob
    where ob.source = o.source and ob.offer_id = o.offer_id)
