// hello_mod: a no-op that only proves the bundling, staging, mixin and compile
// path works. Placed at the HEAD of `native_hud`; does nothing to the HUD.
// Types must resolve in both trees that compile game/ (the horizon-client bin
// and the Android cdylib): `horizon_client::…` is the re-exported lib.
fn run(
    batch: &mut horizon_client::hud::HudBatch,
    corners: &mut horizon_client::extras_hud::Corners,
) {
    let _ = (batch, corners);
}
