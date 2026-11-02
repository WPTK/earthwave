/* A virtual pointer that stays on the seat long enough for the client to bind wl_pointer and see an enter event.
 * `wlrctl pointer click` creates and destroys its pointer within milliseconds, so foot never gets the click under a
 * headless sway (no real input devices). Usage: vptr <x> <y> <width> <height>   (absolute position in the output)
 * Build: wayland-scanner client-header/private-code on wlr-virtual-pointer-unstable-v1.xml, then gcc vptr.c ... -lwayland-client
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct zwlr_virtual_pointer_manager_v1 *mgr;
static struct wl_seat *seat;

static void global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
    if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name))
        mgr = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
    else if (!strcmp(iface, wl_seat_interface.name))
        seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
}
static void global_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener rl = {global, global_remove};

static void pause_ms(struct wl_display *dsp, int ms) {
    wl_display_roundtrip(dsp);
    usleep(ms * 1000);
}

int main(int argc, char **argv) {
    if (argc < 5) return 2;
    unsigned x = atoi(argv[1]), y = atoi(argv[2]), w = atoi(argv[3]), h = atoi(argv[4]);
    struct wl_display *dsp = wl_display_connect(NULL);
    if (!dsp) { fprintf(stderr, "no wayland display\n"); return 1; }
    struct wl_registry *reg = wl_display_get_registry(dsp);
    wl_registry_add_listener(reg, &rl, NULL);
    wl_display_roundtrip(dsp);
    if (!mgr || !seat) { fprintf(stderr, "no virtual pointer manager or seat\n"); return 1; }
    struct zwlr_virtual_pointer_v1 *p = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(mgr, seat);
    pause_ms(dsp, 700);                       /* the client sees the pointer capability and binds wl_pointer */
    zwlr_virtual_pointer_v1_motion_absolute(p, 0, x > 40 ? x - 40 : 0, y, w, h);
    zwlr_virtual_pointer_v1_frame(p);
    pause_ms(dsp, 300);
    zwlr_virtual_pointer_v1_motion_absolute(p, 1, x, y, w, h);   /* the pointer moves into the cell before the click */
    zwlr_virtual_pointer_v1_frame(p);
    pause_ms(dsp, 300);
    zwlr_virtual_pointer_v1_button(p, 2, 0x110, WL_POINTER_BUTTON_STATE_PRESSED);   /* BTN_LEFT */
    zwlr_virtual_pointer_v1_frame(p);
    pause_ms(dsp, 100);
    zwlr_virtual_pointer_v1_button(p, 3, 0x110, WL_POINTER_BUTTON_STATE_RELEASED);
    zwlr_virtual_pointer_v1_frame(p);
    pause_ms(dsp, 500);
    zwlr_virtual_pointer_v1_destroy(p);
    wl_display_roundtrip(dsp);
    return 0;
}
