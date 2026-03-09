// SPDX-License-Identifier: GPL-2.0-only
/*
 * BPF fault_ops program for Firecracker live snapshot (bpf_fault).
 *
 * On a write-protect fault, copies the page address and pre-image data
 * into a ring buffer for userspace consumption, then allows the write.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char _license[] SEC("license") = "GPL";

/* Ring buffer for page pre-images. Sized by userspace at map creation. */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 16 * 1024 * 1024); /* default, overridden by loader */
} page_events SEC(".maps");

/* Per-CPU counter for ring buffer reservation failures (dropped pre-images). */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} drop_counter SEC(".maps");

/* Pre-image record: 8 bytes address + 4096 bytes page data = 4104 bytes. */
struct page_event {
	__u64 address;
	__u8 data[4096];
};

SEC("struct_ops/handle_wp_fault")
int BPF_PROG(handle_wp_fault, struct bpf_fault_ops_ctx *ops_ctx,
	     unsigned char *buf)
{
	struct page_event *evt;

	if (!buf)
		return 0;

	evt = bpf_ringbuf_reserve(&page_events, sizeof(*evt), 0);
	if (!evt) {
		/* Ring buffer full — increment drop counter and allow the write.
		 * Userspace detects this and logs a warning about degraded
		 * snapshot consistency. */
		__u32 key = 0;
		__u64 *cnt = bpf_map_lookup_elem(&drop_counter, &key);
		if (cnt)
			__sync_fetch_and_add(cnt, 1);
		return 0;
	}

	evt->address = ops_ctx->address;

	/* Copy the pre-image page data.
	 * Use bpf_probe_read_kernel to copy from the buf pointer,
	 * which satisfies the verifier's null/bounds checking.
	 */
	bpf_probe_read_kernel(evt->data, 4096, buf);

	bpf_ringbuf_submit(evt, BPF_RB_NO_WAKEUP);
	return 0;
}

SEC("struct_ops/handle_page_fault")
int BPF_PROG(handle_page_fault, struct bpf_fault_ops_ctx *ops_ctx,
	     unsigned char *buf)
{
	/* We only care about WP faults; this is a no-op for missing faults. */
	return 0;
}

SEC(".struct_ops.link")
struct fault_ops snapshot_fault_ops = {
	.handle_page_fault = (void *)handle_page_fault,
	.handle_wp_fault = (void *)handle_wp_fault,
};
