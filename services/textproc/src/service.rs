use std::{collections::BTreeMap, num::NonZeroUsize, sync::Arc};

use aho_corasick::{AhoCorasick, AhoCorasickBuilder, MatchKind};
use lru::LruCache;
use sha2::{Digest, Sha256};
use tokio::{sync::Mutex, task};
use tonic::{Request, Response, Status};
use unicode_normalization::UnicodeNormalization;

use crate::pb::{
    Alias, HashRequest, HashResponse, MentionScanRequest, MentionScanResponse, RetroRequest,
    RetroResponse, Span, text_proc_server::TextProc,
};

const MAX_TEXT_BYTES: usize = 2 * 1024 * 1024;
const MAX_ALIASES: usize = 100_000;

struct Matcher {
    automaton: AhoCorasick,
    alias_ids: Vec<Vec<String>>,
    surfaces: Vec<String>,
}

#[derive(Clone)]
pub struct TextProcService {
    matchers: Arc<Mutex<LruCache<String, Arc<Matcher>>>>,
}

impl TextProcService {
    pub fn new(capacity: usize) -> Self {
        let capacity = NonZeroUsize::new(capacity.max(1)).expect("capacity is non-zero");
        Self {
            matchers: Arc::new(Mutex::new(LruCache::new(capacity))),
        }
    }

    async fn matcher(&self, language: &str, aliases: &[Alias]) -> Result<Arc<Matcher>, Status> {
        let mut surfaces: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for alias in aliases {
            if !alias.surface.is_empty() {
                surfaces
                    .entry(alias.surface.clone())
                    .or_default()
                    .push(alias.alias_id.clone());
            }
        }
        for ids in surfaces.values_mut() {
            ids.sort();
            ids.dedup();
        }

        let mut digest = Sha256::new();
        digest.update(language.as_bytes());
        for (surface, ids) in &surfaces {
            digest.update([0x1f]);
            digest.update(surface.as_bytes());
            for id in ids {
                digest.update([0x1f]);
                digest.update(id.as_bytes());
            }
        }
        let key = hex::encode(digest.finalize());
        if let Some(matcher) = self.matchers.lock().await.get(&key).cloned() {
            return Ok(matcher);
        }

        let patterns: Vec<String> = surfaces.keys().cloned().collect();
        let matcher_surfaces = patterns.clone();
        let alias_ids: Vec<Vec<String>> = surfaces.into_values().collect();
        let matcher = task::spawn_blocking(move || {
            AhoCorasickBuilder::new()
                .match_kind(MatchKind::LeftmostLongest)
                .build(&patterns)
                .map(|automaton| {
                    Arc::new(Matcher {
                        automaton,
                        alias_ids,
                        surfaces: matcher_surfaces,
                    })
                })
        })
        .await
        .map_err(|error| Status::internal(format!("matcher task failed: {error}")))?
        .map_err(|error| Status::internal(format!("build matcher: {error}")))?;

        let mut cache = self.matchers.lock().await;
        if let Some(existing) = cache.get(&key).cloned() {
            return Ok(existing);
        }
        cache.put(key, matcher.clone());
        Ok(matcher)
    }

    #[cfg(test)]
    async fn cache_len(&self) -> usize {
        self.matchers.lock().await.len()
    }
}

fn char_offsets(text: &str) -> Vec<u32> {
    let mut offsets = vec![0; text.len() + 1];
    for (char_index, (byte_index, _)) in text.char_indices().enumerate() {
        offsets[byte_index] = char_index as u32;
    }
    offsets[text.len()] = text.chars().count() as u32;
    offsets
}

fn whole_name(text: &str, start: usize, end: usize, surface: &str, language: &str) -> bool {
    if matches!(language.split('-').next(), Some("zh" | "ja" | "ko")) {
        return true;
    }
    let word = |ch: char| ch.is_alphanumeric() || ch == '_';
    let first = surface.chars().next();
    let last = surface.chars().next_back();
    let left = text[..start].chars().next_back();
    let right = text[end..].chars().next();
    !matches!((first, left), (Some(a), Some(b)) if word(a) && word(b))
        && !matches!((last, right), (Some(a), Some(b)) if word(a) && word(b))
}

fn normalized(text: &str) -> String {
    text.nfkc()
        .flat_map(char::to_lowercase)
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn simhash64(text: &str) -> String {
    let normalized = normalized(text);
    let chars: Vec<char> = normalized.chars().collect();
    let shingles: Vec<String> = if chars.len() < 5 {
        vec![normalized]
    } else {
        chars
            .windows(5)
            .map(|window| window.iter().collect())
            .collect()
    };
    let mut weights = [0i32; 64];
    for shingle in shingles {
        let hash = Sha256::digest(shingle.as_bytes());
        let value = u64::from_be_bytes(hash[..8].try_into().expect("sha256 prefix"));
        for (bit, weight) in weights.iter_mut().enumerate() {
            *weight += if value & (1 << bit) != 0 { 1 } else { -1 };
        }
    }
    let value = weights
        .iter()
        .enumerate()
        .fold(0u64, |result, (bit, weight)| {
            if *weight > 0 {
                result | (1 << bit)
            } else {
                result
            }
        });
    format!("simhash64-v1:{value:016x}")
}

#[tonic::async_trait]
impl TextProc for TextProcService {
    async fn scan_mentions(
        &self,
        request: Request<MentionScanRequest>,
    ) -> Result<Response<MentionScanResponse>, Status> {
        let request = request.into_inner();
        if request.text.len() > MAX_TEXT_BYTES || request.aliases.len() > MAX_ALIASES {
            return Err(Status::resource_exhausted(
                "text or alias request exceeds service limit",
            ));
        }
        if request.text.is_empty()
            || request.aliases.is_empty()
            || request.aliases.iter().all(|alias| alias.surface.is_empty())
        {
            return Ok(Response::new(MentionScanResponse { spans: vec![] }));
        }
        let matcher = self.matcher(&request.lang, &request.aliases).await?;
        let offsets = char_offsets(&request.text);
        let mut spans = Vec::new();
        for found in matcher.automaton.find_iter(&request.text) {
            let start = found.start();
            let end = found.end();
            let pattern = found.pattern().as_usize();
            if !whole_name(
                &request.text,
                start,
                end,
                &matcher.surfaces[pattern],
                &request.lang,
            ) {
                continue;
            }
            for alias_id in &matcher.alias_ids[pattern] {
                spans.push(Span {
                    alias_id: alias_id.clone(),
                    byte_start: start as u32,
                    byte_end: end as u32,
                    char_start: offsets[start],
                    char_end: offsets[end],
                });
            }
        }
        Ok(Response::new(MentionScanResponse { spans }))
    }

    async fn hash_content(
        &self,
        request: Request<HashRequest>,
    ) -> Result<Response<HashResponse>, Status> {
        let text = request.into_inner().text;
        if text.len() > MAX_TEXT_BYTES {
            return Err(Status::resource_exhausted(
                "text request exceeds service limit",
            ));
        }
        Ok(Response::new(HashResponse {
            sha256: hex::encode(Sha256::digest(text.as_bytes())),
            near_dup_sig: simhash64(&text),
        }))
    }

    async fn apply_retro_update(
        &self,
        _: Request<RetroRequest>,
    ) -> Result<Response<RetroResponse>, Status> {
        Err(Status::unimplemented(
            "ApplyRetroUpdate is deferred to Milestone 3",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pb::text_proc_server::TextProc;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct GoldenCase {
        name: String,
        text: String,
        lang: String,
        aliases: Vec<(String, String)>,
        spans: Vec<(String, u32, u32, u32, u32)>,
    }

    #[test]
    fn simhash_normalizes_case_and_whitespace() {
        assert_eq!(simhash64("Azure   Cloud"), simhash64("azure cloud"));
    }

    #[test]
    fn offsets_handle_cjk_prefixes() {
        let offsets = char_offsets("第一章：青云宗");
        assert_eq!((offsets[12], offsets[21]), (4, 7));
    }

    #[tokio::test]
    async fn scan_preserves_shared_aliases_and_leftmost_longest() {
        let service = TextProcService::new(2);
        let response = service
            .scan_mentions(Request::new(MentionScanRequest {
                text: "他统治着王国。".to_owned(),
                aliases: vec![
                    Alias {
                        alias_id: "short".to_owned(),
                        surface: "王".to_owned(),
                    },
                    Alias {
                        alias_id: "long-a".to_owned(),
                        surface: "王国".to_owned(),
                    },
                    Alias {
                        alias_id: "long-b".to_owned(),
                        surface: "王国".to_owned(),
                    },
                ],
                lang: "zh".to_owned(),
            }))
            .await
            .expect("scan")
            .into_inner();
        assert_eq!(response.spans.len(), 2);
        assert_eq!(response.spans[0].alias_id, "long-a");
        assert_eq!(response.spans[1].alias_id, "long-b");
        assert_eq!(
            (response.spans[0].byte_start, response.spans[0].byte_end),
            (12, 18)
        );
        assert_eq!(
            (response.spans[0].char_start, response.spans[0].char_end),
            (4, 6)
        );
    }

    #[tokio::test]
    async fn shared_golden_cases_define_the_wire_contract() {
        let cases: Vec<GoldenCase> =
            serde_json::from_str(include_str!("../tests/scan_cases.json")).expect("golden cases");
        let service = TextProcService::new(8);
        for case in cases {
            let response = service
                .scan_mentions(Request::new(MentionScanRequest {
                    text: case.text,
                    aliases: case
                        .aliases
                        .into_iter()
                        .map(|(alias_id, surface)| Alias { alias_id, surface })
                        .collect(),
                    lang: case.lang,
                }))
                .await
                .unwrap_or_else(|error| panic!("{}: {error}", case.name))
                .into_inner();
            let actual: Vec<_> = response
                .spans
                .into_iter()
                .map(|span| {
                    (
                        span.alias_id,
                        span.byte_start,
                        span.byte_end,
                        span.char_start,
                        span.char_end,
                    )
                })
                .collect();
            assert_eq!(actual, case.spans, "{}", case.name);
        }
    }

    #[tokio::test]
    async fn matcher_cache_hits_invalidates_and_evicts() {
        let service = TextProcService::new(1);
        let first_aliases = vec![Alias {
            alias_id: "one".to_owned(),
            surface: "One".to_owned(),
        }];
        let second_aliases = vec![Alias {
            alias_id: "two".to_owned(),
            surface: "Two".to_owned(),
        }];
        let first = service.matcher("en", &first_aliases).await.expect("first");
        let hit = service.matcher("en", &first_aliases).await.expect("hit");
        assert!(Arc::ptr_eq(&first, &hit));
        let second = service
            .matcher("en", &second_aliases)
            .await
            .expect("second");
        assert!(!Arc::ptr_eq(&first, &second));
        assert_eq!(service.cache_len().await, 1);
        let rebuilt = service
            .matcher("en", &first_aliases)
            .await
            .expect("rebuilt");
        assert!(!Arc::ptr_eq(&first, &rebuilt));
    }

    #[tokio::test]
    async fn concurrent_first_use_converges_on_one_cached_matcher() {
        let service = TextProcService::new(2);
        let aliases = vec![Alias {
            alias_id: "one".to_owned(),
            surface: "One".to_owned(),
        }];
        let (left, right) = tokio::join!(
            service.matcher("en", &aliases),
            service.matcher("en", &aliases)
        );
        assert!(Arc::ptr_eq(&left.expect("left"), &right.expect("right")));
        assert_eq!(service.cache_len().await, 1);
    }

    #[tokio::test]
    async fn hash_is_exact_and_retro_is_deferred() {
        let service = TextProcService::new(1);
        let response = service
            .hash_content(Request::new(HashRequest {
                text: "abc".to_owned(),
            }))
            .await
            .expect("hash")
            .into_inner();
        assert_eq!(
            response.sha256,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert!(response.near_dup_sig.starts_with("simhash64-v1:"));
        assert_eq!(
            service
                .apply_retro_update(Request::new(RetroRequest::default()))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unimplemented
        );
    }

    #[tokio::test]
    async fn request_limits_are_resource_exhausted() {
        let service = TextProcService::new(1);
        let error = service
            .scan_mentions(Request::new(MentionScanRequest {
                text: "x".repeat(MAX_TEXT_BYTES + 1),
                aliases: vec![],
                lang: "en".to_owned(),
            }))
            .await
            .unwrap_err();
        assert_eq!(error.code(), tonic::Code::ResourceExhausted);
    }
}
