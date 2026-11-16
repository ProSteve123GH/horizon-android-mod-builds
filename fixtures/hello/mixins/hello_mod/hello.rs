// hello_mod: a no-op that only proves the bundling, staging, mixin and compile
// path works. Placed at the HEAD of `native_hud`; does nothing to the HUD.
fn run(batch: &mut crate::hud::HudBatch, corners: &mut crate::extras_hud::Corners) {
    let _ = (batch, corners);
}
