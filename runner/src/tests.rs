use super::*;

/// Every variant, so a misspelt name fails here. `Debug` starts with the variant's name; the
/// next character must end it, or `CostModelLifecycle` named `CostModel` would pass.
#[test]
fn stop_names_are_the_variant_names() {
    for stop in [
        Stop::MaxInsns,
        Stop::Halted,
        Stop::Breakpoint(0x4008_0000),
        Stop::Unimplemented(0x4008_0000, 0x12),
        Stop::SwReset,
        Stop::Simcall(1),
        Stop::Ebreak(0x4008_0000),
        Stop::Watch(0x3ffb_0000, 0, 1),
        Stop::Exceptions(3),
        Stop::CostModel {
            core: 0,
            pc: 0,
            reason: String::new(),
        },
        Stop::CostModelLifecycle {
            kind: emu_core::LifecycleKind::ChipReset,
            reason: String::new(),
        },
    ] {
        let debug = format!("{stop:?}");
        let name = stop_name(&stop);
        let rest = debug.strip_prefix(name);
        assert!(
            rest.is_some_and(|r| r.is_empty() || r.starts_with([' ', '('])),
            "{debug} named {name}"
        );
    }
}
