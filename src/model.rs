use bincode::{Decode, Encode};
use cu29::prelude::Reflect;
use cu29::reflect as bevy_reflect;
use serde::{Deserialize, Serialize};

#[derive(
    Clone, Copy, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect,
)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect,
)]
pub struct Obstacle {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
    pub dynamic: bool,
}
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect,
)]
pub struct Robot {
    pub x: f64,
    pub y: f64,
    pub yaw: f64,
    pub radius: f64,
}
impl Robot {
    pub fn point(&self) -> Point {
        Point {
            x: self.x,
            y: self.y,
        }
    }
}
#[derive(Clone, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect)]
pub struct Goal {
    pub x: f64,
    pub y: f64,
    pub label: String,
    pub index: usize,
}
impl Goal {
    pub fn point(&self) -> Point {
        Point {
            x: self.x,
            y: self.y,
        }
    }
}
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect,
)]
pub struct Command {
    pub v: f64,
    pub omega: f64,
}
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect,
)]
pub struct World {
    pub width: f64,
    pub height: f64,
}
#[derive(Clone, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect)]
pub struct Sensor {
    pub tick: u64,
    pub time_s: f64,
    pub robot: Robot,
    pub goal: Goal,
    pub goals_completed: u64,
    pub obstacles: Vec<Obstacle>,
    pub collisions: u64,
}
#[derive(Clone, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect)]
pub struct Plan {
    pub sensor: Sensor,
    pub path: Vec<Point>,
    pub status: String,
}
#[derive(Clone, Debug, Default, PartialEq, Encode, Decode, Serialize, Deserialize, Reflect)]
pub struct Frame {
    pub tick: u64,
    pub time_s: f64,
    pub robot: Robot,
    pub goal: Goal,
    pub goals_completed: u64,
    pub path: Vec<Point>,
    pub obstacles: Vec<Obstacle>,
    pub command: Command,
    pub safety_stop: bool,
    pub nearest_obstacle_m: f64,
    pub planner_status: String,
    pub world: World,
    pub pipeline_us: f64,
    pub collisions: u64,
}

pub fn control(plan: &Plan) -> Frame {
    let s = &plan.sensor;
    let mut command = Command::default();
    let mut safety_stop = plan.path.len() < 2;
    if let Some(target) = plan.path.get(1) {
        let error = wrap_angle((target.y - s.robot.y).atan2(target.x - s.robot.x) - s.robot.yaw);
        command.omega = (3.0 * error).clamp(-2.2, 2.2);
        command.v = if error.abs() > 0.8 {
            0.0
        } else {
            0.95 * error.cos().max(0.0)
        };
        // Forward integrate a conservative half-second stopping horizon.
        let mut predicted = s.robot;
        for _ in 0..10 {
            predicted = integrate(predicted, command, DT);
            if clearance(predicted.point(), &s.obstacles) < RADIUS + 0.10 {
                safety_stop = true;
                break;
            }
        }
    }
    if safety_stop {
        command = Command::default();
    }
    Frame {
        tick: s.tick,
        time_s: s.time_s,
        robot: s.robot,
        goal: s.goal.clone(),
        goals_completed: s.goals_completed,
        path: plan.path.clone(),
        obstacles: s.obstacles.clone(),
        command,
        safety_stop,
        nearest_obstacle_m: (clearance(s.robot.point(), &s.obstacles) - RADIUS).max(0.0),
        planner_status: plan.status.clone(),
        world: World {
            width: WIDTH,
            height: HEIGHT,
        },
        pipeline_us: 0.0,
        collisions: s.collisions,
    }
}

pub fn wrap_angle(a: f64) -> f64 {
    (a + std::f64::consts::PI).rem_euclid(std::f64::consts::TAU) - std::f64::consts::PI
}
pub fn integrate(mut robot: Robot, command: Command, dt: f64) -> Robot {
    let mid = robot.yaw + command.omega * dt * 0.5;
    robot.x += command.v * mid.cos() * dt;
    robot.y += command.v * mid.sin() * dt;
    robot.yaw = wrap_angle(robot.yaw + command.omega * dt);
    robot
}
pub const WIDTH: f64 = 12.0;
pub const HEIGHT: f64 = 9.0;
pub const RADIUS: f64 = 0.22;
pub const DT: f64 = 0.05;
const GRID: f64 = 0.25;
const CLEARANCE: f64 = RADIUS + 0.18;

pub fn distance(a: Point, b: Point) -> f64 {
    (a.x - b.x).hypot(a.y - b.y)
}
pub fn clearance(p: Point, obstacles: &[Obstacle]) -> f64 {
    obstacles
        .iter()
        .map(|o| {
            let dx = (o.x - p.x).max(0.0).max(p.x - o.x - o.w);
            let dy = (o.y - p.y).max(0.0).max(p.y - o.y - o.h);
            dx.hypot(dy)
        })
        .fold(p.x.min(p.y).min(WIDTH - p.x).min(HEIGHT - p.y), f64::min)
}
pub fn segment_clear(a: Point, b: Point, obstacles: &[Obstacle], margin: f64) -> bool {
    let n = (distance(a, b) / 0.04).ceil().max(1.0) as usize;
    (0..=n).all(|i| {
        let t = i as f64 / n as f64;
        clearance(
            Point {
                x: a.x + (b.x - a.x) * t,
                y: a.y + (b.y - a.y) * t,
            },
            obstacles,
        ) > margin
    })
}

/// Deterministic 4-connected A* over an obstacle-inflated occupancy grid.
pub fn astar(start: Point, goal: Point, obstacles: &[Obstacle]) -> Vec<Point> {
    use std::cmp::Reverse;
    use std::collections::BinaryHeap;
    const NX: usize = 49;
    const NY: usize = 37;
    let cell = |p: Point| -> usize {
        (p.y / GRID).round().clamp(0.0, (NY - 1) as f64) as usize * NX
            + (p.x / GRID).round().clamp(0.0, (NX - 1) as f64) as usize
    };
    let point = |i: usize| Point {
        x: (i % NX) as f64 * GRID,
        y: (i / NX) as f64 * GRID,
    };
    let s = cell(start);
    let g = cell(goal);
    if clearance(start, obstacles) <= CLEARANCE || clearance(goal, obstacles) <= CLEARANCE {
        return vec![];
    }
    let h = |i: usize| -> usize { (i % NX).abs_diff(g % NX) + (i / NX).abs_diff(g / NX) };
    let mut costs = vec![usize::MAX; NX * NY];
    let mut parent = vec![usize::MAX; NX * NY];
    let mut open = BinaryHeap::new();
    costs[s] = 0;
    open.push(Reverse((h(s), 0, s)));
    while let Some(Reverse((_, cost, i))) = open.pop() {
        if cost != costs[i] {
            continue;
        }
        if i == g {
            let mut path = vec![goal];
            let mut cur = g;
            while cur != s {
                path.push(point(cur));
                cur = parent[cur];
            }
            path.push(start);
            path.reverse();
            let mut smoothed = vec![start];
            let mut a = 0;
            while a + 1 < path.len() {
                let b = ((a + 1)..path.len())
                    .rev()
                    .find(|&b| segment_clear(path[a], path[b], obstacles, CLEARANCE))
                    .unwrap_or(a + 1);
                smoothed.push(path[b]);
                a = b;
            }
            return smoothed;
        }
        let x = i % NX;
        let y = i / NX;
        for (dx, dy) in [(1isize, 0isize), (0, 1), (-1, 0), (0, -1)] {
            let nx = x as isize + dx;
            let ny = y as isize + dy;
            if nx < 0 || ny < 0 || nx >= NX as isize || ny >= NY as isize {
                continue;
            }
            let j = ny as usize * NX + nx as usize;
            if clearance(point(j), obstacles) <= CLEARANCE {
                continue;
            }
            if cost + 1 < costs[j] {
                costs[j] = cost + 1;
                parent[j] = i;
                open.push(Reverse((cost + 1 + h(j), cost + 1, j)));
            }
        }
    }
    vec![]
}

#[derive(Clone, Debug, Encode, Decode)]
pub struct Simulation {
    pub robot: Robot,
    pub tick: u64,
    pub goals_completed: u64,
    pub last_command: Command,
    pub collisions: u64,
}
impl Default for Simulation {
    fn default() -> Self {
        Self {
            robot: Robot {
                x: 1.0,
                y: 1.0,
                yaw: 0.0,
                radius: RADIUS,
            },
            tick: 0,
            goals_completed: 0,
            last_command: Command::default(),
            collisions: 0,
        }
    }
}
pub fn obstacles_at(time_s: f64) -> Vec<Obstacle> {
    vec![
        Obstacle {
            x: 3.2,
            y: 2.0,
            w: 1.2,
            h: 4.0,
            dynamic: false,
        },
        Obstacle {
            x: 7.0,
            y: 3.2,
            w: 1.2,
            h: 3.8,
            dynamic: false,
        },
        Obstacle {
            x: 5.5,
            y: 1.5 + 0.35 * (time_s * 0.5).sin(),
            w: 0.7,
            h: 0.8,
            dynamic: true,
        },
    ]
}
pub fn goal_for(completed: u64) -> Goal {
    let index = completed as usize % 4;
    let (x, y, label) = [
        (10.5, 1.0, "Receiving"),
        (10.5, 6.0, "Dispatch"),
        (1.0, 6.0, "Storage"),
        (1.0, 1.0, "Charging"),
    ][index];
    Goal {
        x,
        y,
        label: label.into(),
        index,
    }
}
impl Simulation {
    /// The source consumes only the previous cycle's command; no wall-clock physics.
    pub fn sense(&mut self) -> Sensor {
        if self.tick > 0 {
            self.robot = integrate(self.robot, self.last_command, DT);
        }
        let time_s = self.tick as f64 * DT;
        let obstacles = obstacles_at(time_s);
        if clearance(self.robot.point(), &obstacles) <= RADIUS {
            self.collisions += 1;
        }
        if distance(self.robot.point(), goal_for(self.goals_completed).point()) < 0.24 {
            self.goals_completed += 1;
        }
        let sensor = Sensor {
            tick: self.tick,
            time_s,
            robot: self.robot,
            goal: goal_for(self.goals_completed),
            goals_completed: self.goals_completed,
            obstacles,
            collisions: self.collisions,
        };
        self.tick += 1;
        sensor
    }
}
pub fn plan(sensor: Sensor) -> Plan {
    let path = astar(sensor.robot.point(), sensor.goal.point(), &sensor.obstacles);
    let status = if path.is_empty() {
        "no_path"
    } else {
        "tracking"
    }
    .into();
    Plan {
        sensor,
        path,
        status,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mission_requires_actual_static_and_dynamic_obstacle_detours() {
        let mut sim = Simulation::default();
        let mut detoured_static = false;
        let mut detoured_dynamic = false;
        for _ in 0..1200 {
            let f = control(&plan(sim.sense()));
            for dynamic in [false, true] {
                let obstacles: Vec<_> = f
                    .obstacles
                    .iter()
                    .copied()
                    .filter(|o| o.dynamic == dynamic)
                    .collect();
                if !segment_clear(f.robot.point(), f.goal.point(), &obstacles, RADIUS)
                    && f.path.len() > 2
                {
                    if dynamic {
                        detoured_dynamic = true;
                    } else {
                        detoured_static = true;
                    }
                    assert!(f.path.windows(2).all(|s| segment_clear(
                        s[0],
                        s[1],
                        &f.obstacles,
                        RADIUS
                    )));
                }
            }
            sim.last_command = f.command;
        }
        assert!(
            detoured_static,
            "mission must route around a static shelf, not just trace the warehouse boundary"
        );
        assert!(
            detoured_dynamic,
            "mission must route around the moving obstruction"
        );
    }
    #[test]
    fn repeated_missions_reach_multiple_goals_without_collisions() {
        let mut previous = None;
        for _ in 0..3 {
            let mut sim = Simulation::default();
            let mut trace = Vec::new();
            for _ in 0..1200 {
                let f = control(&plan(sim.sense()));
                assert_eq!(f.collisions, 0, "tick {} pose {:?}", f.tick, f.robot);
                assert!((0.0..=1.0).contains(&f.command.v));
                assert!(f.command.omega.abs() <= 2.2);
                if f.tick == 799 {
                    assert!(
                        f.goals_completed >= 1,
                        "800-step mission did not reach a goal: {:?}",
                        f
                    );
                }
                sim.last_command = f.command;
                trace.push(f);
            }
            assert!(
                trace.last().unwrap().goals_completed >= 2,
                "1200-step mission: {:?}",
                trace.last()
            );
            assert!(trace.iter().any(|f| f.obstacles.iter().any(|o| o.dynamic)));
            assert!(trace.iter().any(|f| f.path.len() > 2));
            if let Some(p) = previous.as_ref() {
                assert_eq!(&trace, p);
            } else {
                previous = Some(trace);
            }
        }
    }
    #[test]
    fn controller_follows_open_path_with_bounded_velocity() {
        let s = Sensor {
            robot: Robot {
                x: 1.0,
                y: 1.0,
                yaw: 0.0,
                radius: RADIUS,
            },
            goal: Goal {
                x: 10.5,
                y: 1.0,
                ..Goal::default()
            },
            ..Sensor::default()
        };
        let p = Plan {
            path: astar(s.robot.point(), s.goal.point(), &[]),
            sensor: s,
            status: "tracking".into(),
        };
        let f = control(&p);
        assert!(f.command.v > 0.0 && f.command.v <= 1.0);
        assert!(f.command.omega.abs() <= 2.2);
        assert!(!f.safety_stop);
    }
    #[test]
    fn blocked_world_stops_controller() {
        let s = Sensor {
            robot: Robot {
                x: 1.0,
                y: 1.0,
                yaw: 0.0,
                radius: RADIUS,
            },
            goal: Goal {
                x: 10.0,
                y: 1.0,
                ..Goal::default()
            },
            obstacles: vec![Obstacle {
                x: 4.0,
                y: 0.0,
                w: 1.0,
                h: HEIGHT,
                dynamic: false,
            }],
            ..Sensor::default()
        };
        let path = astar(s.robot.point(), s.goal.point(), &s.obstacles);
        assert!(path.is_empty());
        let frame = control(&Plan {
            sensor: s,
            path,
            status: "no_path".into(),
        });
        assert!(frame.safety_stop);
        assert_eq!(frame.command, Command::default());
    }
    #[test]
    fn astar_routes_around_inflated_warehouse_shelf() {
        let shelf = Obstacle {
            x: 3.0,
            y: 0.5,
            w: 1.0,
            h: 5.0,
            dynamic: false,
        };
        let path = astar(Point { x: 1.0, y: 1.0 }, Point { x: 6.0, y: 1.0 }, &[shelf]);
        assert!(!path.is_empty());
        assert!(path.iter().any(|p| p.y > 5.8));
        for p in path {
            assert!(p.x < 2.7 || p.x > 4.3 || p.y < 0.2 || p.y > 5.8);
        }
    }
}
