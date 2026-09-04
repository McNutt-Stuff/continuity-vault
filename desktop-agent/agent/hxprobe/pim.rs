//! Best-effort extraction of contact and calendar records from verified blocks.
//! These mappings are intentionally marked partial until their positional
//! schemas have been validated against protocol traces as the mail schema was.

use crate::record;

#[derive(Debug)]
pub struct Contact {
    pub block: usize,
    pub item_class: String,
    pub display_name: Option<String>,
    pub emails: Vec<String>,
    pub phones: Vec<String>,
    pub raw_fields: Vec<String>,
    pub modified_unix: Option<i64>,
}

#[derive(Debug)]
pub struct Event {
    pub block: usize,
    pub item_class: String,
    pub title: Option<String>,
    pub people: Vec<String>,
    pub body: Option<String>,
    pub raw_fields: Vec<String>,
    pub start_unix: Option<i64>,
    pub end_unix: Option<i64>,
}

fn wide(s: &str) -> Vec<u8> {
    s.encode_utf16().flat_map(|u| u.to_le_bytes()).collect()
}

fn phone_like(s: &str) -> bool {
    let digits = s.chars().filter(|c| c.is_ascii_digit()).count();
    (7..=18).contains(&digits)
        && s.chars().all(|c| c.is_ascii_digit() || " +()-./xXextEXT".contains(c))
}

fn unique(mut values: Vec<String>) -> Vec<String> {
    values.sort_by_key(|v| v.to_ascii_lowercase());
    values.dedup_by(|a, b| a.eq_ignore_ascii_case(b));
    values
}

fn occurrences(data: &[u8], prefixes: &[&str]) -> Vec<(usize, String)> {
    let mut found = Vec::new();
    for prefix in prefixes {
        let needle = wide(prefix);
        for at in memchr::memmem::find_iter(data, &needle) {
            found.push((at, (*prefix).to_string()));
        }
    }
    found.sort_by_key(|x| x.0);
    found.dedup_by_key(|x| x.0);
    found
}

fn bounded_fields(data: &[u8], anchors: &[(usize, String)], i: usize) -> (Vec<String>, Vec<i64>) {
    let at = anchors[i].0;
    let lo = if i == 0 { at.saturating_sub(4096) } else { anchors[i - 1].0 };
    let hi = anchors.get(i + 1).map_or(data.len(), |x| x.0);
    let fields = record::fields(data, at, at - lo, hi - at)
        .into_iter().map(|f| record::undouble_pub(f.text.trim())).collect();
    (unique(fields), record::read_times(&data[lo..hi]))
}

pub fn contacts(block: usize, data: &[u8]) -> Vec<Contact> {
    let anchors = occurrences(data, &["IPM.Contact"]);
    anchors.iter().enumerate().map(|(i, (_, class))| {
        let (raw, times) = bounded_fields(data, &anchors, i);
        let emails = unique(raw.iter().filter(|s| record::looks_like_email(s)).cloned().collect());
        let phones = unique(raw.iter().filter(|s| phone_like(s)).cloned().collect());
        let display_name = raw.iter().find(|s| {
            !s.starts_with("IPM.") && !record::looks_like_email(s) && !phone_like(s)
                && record::looks_like_person(s)
        }).cloned();
        Contact { block, item_class: class.clone(), display_name, emails, phones,
            raw_fields: raw, modified_unix: times.last().copied() }
    }).collect()
}

pub fn events(block: usize, data: &[u8]) -> Vec<Event> {
    let anchors = occurrences(data, &[
        "IPM.Appointment", "IPM.Schedule.Meeting.Request",
        "IPM.Schedule.Meeting.Resp.Pos", "IPM.Schedule.Meeting.Resp.Neg",
        "IPM.Schedule.Meeting.Resp.Tent",
    ]);
    anchors.iter().enumerate().map(|(i, (_, class))| {
        let (raw, times) = bounded_fields(data, &anchors, i);
        let people = unique(raw.iter().filter(|s| record::looks_like_email(s)).cloned().collect());
        let mut candidates: Vec<String> = raw.iter().filter(|s| {
            !s.starts_with("IPM.") && !record::looks_like_email(s) && record::subject_like(s)
        }).cloned().collect();
        let title = candidates.first().cloned();
        candidates.sort_by_key(|s| std::cmp::Reverse(s.len()));
        let body = candidates.into_iter().find(|s| Some(s) != title.as_ref() && s.len() > 40);
        Event { block, item_class: class.clone(), title, people, body, raw_fields: raw,
            start_unix: times.first().copied(), end_unix: times.get(1).copied() }
    }).collect()
}
