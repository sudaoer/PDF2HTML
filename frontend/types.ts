export type ReaderMode = "bilingual" | "target" | "source";

export interface PaperMetadata {
  title: string;
  creators: string[];
  date: string;
  publication: string;
  doi: string;
  url: string;
  abstract: string;
  tags: string[];
}

export interface Segment {
  id: string;
  role: string;
  source: string;
  source_hash: string;
  protected: string[];
}

export interface FigureMember {
  id: string;
  asset: string;
  label: string;
  page: number;
  bbox: number[];
}

export interface DocumentBlock {
  id: string;
  kind: string;
  page: number;
  level: number;
  source: string;
  segment_id: string;
  asset: string;
  caption_source: string;
  caption_segment_id: string;
  footnote_source: string;
  footnote_segment_id: string;
  members: FigureMember[];
  layout: "auto" | "row" | "column" | "grid";
  extra: Record<string, unknown>;
}

export interface DocumentNote {
  id: string;
  kind: "author_note" | "copyright" | "endnote";
  marker: string;
  page: number;
  source: string;
  segment_id: string;
  url: string;
}

export interface PaperDocument {
  job_id: string;
  metadata: PaperMetadata;
  blocks: DocumentBlock[];
  segments: Segment[];
  front_matter_notes: DocumentNote[];
  endnotes: DocumentNote[];
}

export interface BundleTranslation {
  id: string;
  source_hash: string;
  target: string;
}

export interface BundleMedia {
  asset: string;
  path: string;
  sha256: string;
  size: number;
  media_type: string;
}

export interface PaperBundle {
  schema_version: 1;
  bundle_revision: string;
  paper_id: string;
  job_id: string;
  document: PaperDocument;
  translations: BundleTranslation[];
  media: BundleMedia[];
}

export interface ShelfPaper extends PaperMetadata {
  paper_id: string;
  job_id: string;
  title_zh: string;
  href: string;
  bundle_revision: string;
}

export interface ShelfError {
  paper_id: string;
  code: "invalid_bundle";
  message: string;
}

export interface ShelfResponse {
  schema_version: 1;
  library_revision: string;
  papers: ShelfPaper[];
  errors: ShelfError[];
}

export interface SearchResult {
  paper_id: string;
  bundle_revision: string;
  kind: string;
  block_id: string | null;
  segment_id: string | null;
  page: number | null;
  title: string;
  title_zh: string;
  source_excerpt: string;
  target_excerpt: string;
  score: number;
}

export interface SearchResponse {
  query: string;
  library_revision: string;
  results: SearchResult[];
}
