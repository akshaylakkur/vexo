// Shapes of what steadyd sends on /ws/live (see src/steady/screening/screener.py).

export type Context = "rest" | "active" | "walking" | "not_worn" | "unknown";

export interface WindowRow {
  t_start: number;
  t_end: number;
  valid: 0 | 1;
  valid_frac: number;
  n_samples: number;
  lost_samples: number;
  context: Context;
  activity_state: string | null;
  cadence: number | null;
  steps_delta: number | null;
  acc_dyn_rms?: number | null;
  gyro_rms?: number | null;
  vol_rms?: number | null;
  total_rms?: number | null;
  dom_freq?: number | null;
  peak_prom?: number | null;
  rest_ratio?: number | null;
  action_ratio?: number | null;
  tremor_amp?: number | null;
  vigor?: number | null;
  flags: number;
  battery: number | null;
  rssi: number | null;
  spectrum?: { df: number; psd: number[] } | null;
  data_age_s?: number | null;
  compute_ms?: number;
  detections?: string[];
  checks?: Record<string, boolean> | null;
}

export interface Episode {
  id: number;
  kind: string;
  status?: string;
  started_at: number;
  ended_at: number | null;
  duration_s: number | null;
  severity: string;
  metrics: Record<string, number | string | boolean | null>;
}

export interface ConversationTurn {
  id: number | null;
  role: "assistant" | "user" | "event";
  text: string;
  final: boolean;
  latency_ms?: number | null;
  interrupted?: boolean;
}

export interface BandStatus {
  connected: boolean;
  reconnecting: boolean;
  name: string | null;
  firmware: string | null;
  transport: string | null;
  battery: number | null;
  charging: boolean | null;
  rssi: number | null;
  last_error: string | null;
  samples: number;
  lost_samples: number;
}

export interface BandEvent {
  kind: string;
  time: number;
  arg: number | null;
}

export type LiveMessage =
  | { topic: "window"; data: WindowRow }
  | { topic: "band.status"; data: BandStatus }
  | { topic: "band.event"; data: BandEvent }
  | { topic: "episode.started" | "episode.escalated" | "episode.ended"; data: Episode }
  | { topic: "conversation.state"; data: { id: number | null; state: string; trigger: string } }
  | { topic: "conversation.turn"; data: ConversationTurn };
