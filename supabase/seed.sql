insert into public.practices(name,slug,timezone)
values('Rausch PT & Wellness','rausch-pt','America/Los_Angeles')
on conflict(slug) do update set name=excluded.name,timezone=excluded.timezone;

insert into public.practice_settings(
  practice_id,vapi_assistant_id,vapi_phone_number_id,twilio_from_number,booking_link_url,
  stride_location_id,stride_clinician_ids,stride_appointment_type_id,stride_default_duration_mins,
  stride_case_title,stride_location_timezone,stride_booking_enabled
)
select id,null,null,'+15550000001','https://example.test/book',3169,'5981,5982,5980',1452,60,
  'Initial Evaluation','America/New_York',false
from public.practices where slug='rausch-pt'
on conflict(practice_id) do update set
  twilio_from_number=excluded.twilio_from_number,booking_link_url=excluded.booking_link_url,
  stride_location_id=excluded.stride_location_id,stride_clinician_ids=excluded.stride_clinician_ids,
  stride_appointment_type_id=excluded.stride_appointment_type_id,
  stride_default_duration_mins=excluded.stride_default_duration_mins,
  stride_case_title=excluded.stride_case_title,
  stride_location_timezone=excluded.stride_location_timezone,
  stride_booking_enabled=excluded.stride_booking_enabled;

insert into public.cadence_versions(practice_id,version_number,name,status,activated_at)
select id,3,'Standard v3','active',now() from public.practices where slug='rausch-pt'
on conflict (practice_id,version_number) where lead_id is null do update set
  name=excluded.name,status='active',activated_at=coalesce(public.cadence_versions.activated_at,now());

with p as (
  select p.id,cv.id as cadence_version_id from public.practices p
  join public.cadence_versions cv on cv.practice_id=p.id and cv.lead_id is null
  where p.slug='rausch-pt' and cv.version_number=3
), values_to_upsert as (
  select * from (values
    (0,0,'call','day0_call','Day 0 initial scheduling call'),
    (1,0,'sms','day0_sms','Day 0 introduction and booking link'),
    (2,1,'sms','day1_sms','Day 1 booking reminder'),
    (3,3,'call','day3_call','Day 3 scheduling call'),
    (4,5,'call','day5_call','Day 5 scheduling call'),
    (5,5,'sms','day5_sms','Day 5 encouragement and booking link'),
    (6,9,'sms','day9_sms','Day 9 reply CALL or use booking link'),
    (7,13,'sms','day13_sms','Day 13 final reminder')
  ) as v(step_order,day_offset,channel,key,description)
)
insert into public.cadence_steps(practice_id,cadence_version_id,step_order,day_offset,channel,key,description)
select p.id,p.cadence_version_id,v.step_order,v.day_offset,v.channel,v.key,v.description
from p cross join values_to_upsert v
on conflict(cadence_version_id,key) do update set step_order=excluded.step_order,day_offset=excluded.day_offset,
  channel=excluded.channel,description=excluded.description,is_active=true;

with p as (
  select p.id,cv.id as cadence_version_id from public.practices p
  join public.cadence_versions cv on cv.practice_id=p.id and cv.lead_id is null
  where p.slug='rausch-pt' and cv.version_number=3
), templates as (
  select * from (values
    ('day0_sms','Hi {name}, Rausch PT & Wellness received a request to help you schedule a physical therapy evaluation. Book here: {link}. Reply STOP to opt out.'),
    ('day1_sms','Hi {name}, just a friendly reminder to schedule your physical therapy evaluation: {link} or call 949-276-5401. Reply STOP to opt out.'),
    ('day5_sms','Hi {name}, getting started with physical therapy is the first step toward moving with less pain and more confidence. Schedule here: {link}. Reply STOP to opt out.'),
    ('day9_sms','Hi {name}, our physical therapy team is here to help. Reply CALL for help scheduling or book here: {link}. Reply STOP to opt out.'),
    ('day13_sms','Hi {name}, this is a final reminder from Rausch PT & Wellness. Book here: {link} or call 949-276-5401. Reply STOP to opt out.')
  ) as t(key,body)
)
insert into public.message_templates(practice_id,cadence_version_id,cadence_step_id,key,channel,body)
select p.id,p.cadence_version_id,cs.id,t.key,'sms',t.body from p
join public.cadence_steps cs on cs.cadence_version_id=p.cadence_version_id
join templates t on t.key=cs.key
on conflict(cadence_version_id,key) where cadence_version_id is not null do update set
  cadence_step_id=excluded.cadence_step_id,body=excluded.body,is_active=true;
