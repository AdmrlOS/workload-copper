//! Copper tasks. The only feedback is an in-process simulated command.
use crate::model::*;
use cu29::prelude::*;
use cu29::reflect as bevy_reflect;
use std::cell::RefCell;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::time::Instant;

pub struct RunState {
    pub simulation: Simulation,
    pub writer: Option<BufWriter<File>>,
    pub last_frame: Option<Frame>,
    pub cycle_start: Instant,
}
impl Default for RunState {
    fn default() -> Self {
        Self {
            simulation: Simulation::default(),
            writer: None,
            last_frame: None,
            cycle_start: Instant::now(),
        }
    }
}
thread_local! { pub static STATE: RefCell<RunState> = RefCell::new(RunState::default()); }

#[cfg(test)]
thread_local! { pub(crate) static TEST_COUNTS: std::cell::Cell<[u64;4]> = const { std::cell::Cell::new([0;4]) }; }

#[derive(Reflect)]
pub struct SimSensors;
impl Freezable for SimSensors {
    fn freeze<E: bincode::enc::Encoder>(
        &self,
        encoder: &mut E,
    ) -> std::result::Result<(), bincode::error::EncodeError> {
        STATE.with_borrow(|s| bincode::Encode::encode(&s.simulation, encoder))
    }
    fn thaw<D: bincode::de::Decoder>(
        &mut self,
        decoder: &mut D,
    ) -> std::result::Result<(), bincode::error::DecodeError> {
        let simulation = bincode::Decode::decode(decoder)?;
        STATE.with_borrow_mut(|s| s.simulation = simulation);
        Ok(())
    }
}
impl CuSrcTask for SimSensors {
    type Resources<'r> = ();
    type Output<'m> = output_msg!(Sensor);
    fn new(_: Option<&ComponentConfig>, _: Self::Resources<'_>) -> CuResult<Self> {
        Ok(Self)
    }
    fn process(&mut self, ctx: &CuContext, output: &mut Self::Output<'_>) -> CuResult<()> {
        #[cfg(test)]
        TEST_COUNTS.update(|mut n| {
            n[0] += 1;
            n
        });
        let sensor = STATE.with_borrow_mut(|s| {
            s.cycle_start = Instant::now();
            s.simulation.sense()
        });
        output.set_payload(sensor);
        output.tov = Tov::Time(ctx.now());
        Ok(())
    }
}
#[derive(Reflect)]
pub struct Navigator;
impl Freezable for Navigator {}
impl CuTask for Navigator {
    type Resources<'r> = ();
    type Input<'m> = input_msg!(Sensor);
    type Output<'m> = output_msg!(Plan);
    fn new(_: Option<&ComponentConfig>, _: Self::Resources<'_>) -> CuResult<Self> {
        Ok(Self)
    }
    fn process(
        &mut self,
        _: &CuContext,
        input: &Self::Input<'_>,
        output: &mut Self::Output<'_>,
    ) -> CuResult<()> {
        #[cfg(test)]
        TEST_COUNTS.update(|mut n| {
            n[1] += 1;
            n
        });
        let sensor = input
            .payload()
            .ok_or(CuError::from("sensor payload missing"))?;
        output.set_payload(plan(sensor.clone()));
        output.tov = input.tov;
        Ok(())
    }
}
#[derive(Reflect)]
pub struct SafetyController;
impl Freezable for SafetyController {}
impl CuTask for SafetyController {
    type Resources<'r> = ();
    type Input<'m> = input_msg!(Plan);
    type Output<'m> = output_msg!(Frame);
    fn new(_: Option<&ComponentConfig>, _: Self::Resources<'_>) -> CuResult<Self> {
        Ok(Self)
    }
    fn process(
        &mut self,
        _: &CuContext,
        input: &Self::Input<'_>,
        output: &mut Self::Output<'_>,
    ) -> CuResult<()> {
        #[cfg(test)]
        TEST_COUNTS.update(|mut n| {
            n[2] += 1;
            n
        });
        let p = input
            .payload()
            .ok_or(CuError::from("planner payload missing"))?;
        output.set_payload(control(p));
        output.tov = input.tov;
        Ok(())
    }
}
#[derive(Reflect)]
pub struct TelemetrySink;
impl Freezable for TelemetrySink {}
impl CuSinkTask for TelemetrySink {
    type Resources<'r> = ();
    type Input<'m> = input_msg!(Frame);
    fn new(_: Option<&ComponentConfig>, _: Self::Resources<'_>) -> CuResult<Self> {
        Ok(Self)
    }
    fn process(&mut self, _: &CuContext, input: &Self::Input<'_>) -> CuResult<()> {
        #[cfg(test)]
        TEST_COUNTS.update(|mut n| {
            n[3] += 1;
            n
        });
        let mut frame = input
            .payload()
            .ok_or(CuError::from("controller payload missing"))?
            .clone();
        STATE.with_borrow_mut(|s| -> CuResult<()> {
            frame.pipeline_us = s.cycle_start.elapsed().as_secs_f64() * 1_000_000.0;
            s.simulation.last_command = frame.command;
            if let Some(writer) = s.writer.as_mut() {
                serde_json::to_writer(&mut *writer, &frame)
                    .map_err(|e| CuError::new_with_cause("serialize telemetry", e))?;
                writer
                    .write_all(b"\n")
                    .and_then(|_| writer.flush())
                    .map_err(|e| CuError::new_with_cause("flush telemetry", e))?;
            }
            s.last_frame = Some(frame);
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_keyframe_restores_all_shared_physics_state() {
        use bincode::de::{DecoderImpl, read::SliceReader};
        use bincode::enc::{EncoderImpl, write::SliceWriter};
        let mut source = SimSensors;
        STATE.with_borrow_mut(|s| {
            s.simulation.tick = 123;
            s.simulation.robot.x = 6.75;
            s.simulation.robot.yaw = -1.0;
            s.simulation.goals_completed = 2;
            s.simulation.collisions = 4;
            s.simulation.last_command = Command {
                v: 0.7,
                omega: -0.2,
            };
        });
        let mut bytes = [0u8; 1024];
        source
            .freeze(&mut EncoderImpl::new(
                SliceWriter::new(&mut bytes),
                bincode::config::standard(),
            ))
            .unwrap();
        STATE.with_borrow_mut(|s| s.simulation = Simulation::default());
        source
            .thaw(&mut DecoderImpl::new(
                SliceReader::new(&bytes),
                bincode::config::standard(),
                (),
            ))
            .unwrap();
        STATE.with_borrow(|s| {
            assert_eq!(s.simulation.tick, 123);
            assert_eq!(s.simulation.robot.x, 6.75);
            assert_eq!(s.simulation.robot.yaw, -1.0);
            assert_eq!(s.simulation.goals_completed, 2);
            assert_eq!(s.simulation.collisions, 4);
            assert_eq!(
                s.simulation.last_command,
                Command {
                    v: 0.7,
                    omega: -0.2
                }
            );
        });
    }
}
